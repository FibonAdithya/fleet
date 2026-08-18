"""The allocator's pure core.

A fold, not a stateless function. Parent §11 writes the signature as
decide(ledger, policy, agent_states), but two inputs are in none of those: the
herdr snapshot carries no timestamp anywhere and no issue number, only monotonic
counters and a worktree path. Threading the derived memory explicitly is what
puts the attempt counter and the stall timer under test instead of leaving them
in shell bookkeeping nothing reaches.

No clock, no I/O, no network. Time enters as `now`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fleet import labels as labels_module
from fleet.model import (
    Advance,
    AgentState,
    Dispatch,
    Drain,
    Escalate,
    FleetState,
    Intent,
    Observation,
    Policy,
    Release,
    Retire,
    Task,
    Unblock,
)

# Intents whose expansion stops the agent, so that agent's cost is final.
_REAPING = (Advance, Release, Escalate, Retire)

_DRAIN_FRACTION = 0.8

# States that hold an area and a slot. REVIEW is live because someone is working
# on the task: it keeps its area until `fleet verdict` moves it out, which is the
# only legal exit (spec §5 ruling 4). Nothing here dispatches for those states --
# a reviewer and an integrator are M3's to start.
_LIVE = ("claimed", "review", "integrating")


def _open(ledger: list[Task]) -> list[Task]:
    """The rows a tick may act on. A CLOSED issue is not a task.

    `is_open` is parsed faithfully by `ledger.read_one` and every phase reads
    this filtered list rather than the raw one, so the state label alone can
    never make a closed row hold a slot, hold an area, be reaped, or be
    unblocked. Captured row #12 is the concrete case: CLOSED but still labelled
    `fleet:review` and `area:docs`, so keying on the label alone made it consume
    a slot and lock area:docs for the whole night with nothing able to release
    it -- REAP only touches CLAIMED, and nobody runs `fleet verdict` on a closed
    issue.

    Filtering once, here, is also what keeps the fake and the real path in
    agreement: `fleet plan` reads `gh.list_issues("open")` while the simulator
    seeds from a captured file that includes closed rows, and this makes
    decide() give both the same answer by construction (spec §3's invariant).

    The consequence, stated: a dependency that was closed rather than labelled
    `fleet:done` no longer satisfies UNBLOCK. Nothing in M2 closes an issue --
    `close_issue` is on FakeGh alone and on no Protocol -- so `done` is a label
    on an open row, and treating a closed row as done would be exactly the
    fake/real divergence this filter removes.
    """
    return [task for task in ledger if task.is_open]


def _observe(prev: FleetState, agents: list[AgentState], now: datetime) -> dict[int, Observation]:
    """Advance a task's observation only when its sequence actually moved.

    Prior observations are carried forward rather than rebuilt from this tick's
    agents. A snapshot that momentarily omits an agent -- a herdr query racing a
    pane restart -- would otherwise reset that task's stall timer to zero, and a
    stall that keeps flickering is never detected at all.
    """
    out = dict(prev.seen)
    for agent in agents:
        old = out.get(agent.task)
        if old is None or old.change_seq != agent.change_seq:
            out[agent.task] = Observation(change_seq=agent.change_seq, since=now)
    return out


def _fail(task: Task, reason: str, policy: Policy) -> Intent:
    """One more attempt, or a human. The cap is checked before the release, not
    after: releasing first and counting later is the shape that produces a third
    attempt, which is one of the four hard fails."""
    attempt = task.attempts + 1
    if attempt > policy.max_attempts:
        return Escalate(task=task.number, reason="attempts_exhausted")
    return Release(task=task.number, reason=reason, attempt=attempt)


def _advance(task: Task) -> list[Intent]:
    """A clean exit, routed by its packet.

    No packet at all is not a failure. `gh issue list` does not fetch comment
    bodies, so the shell fetches packets for the states that carry one and the
    next tick advances it; spec §5 calls `packet is None` a fact about the query.
    Releasing here would burn an attempt for a healthy task and escalate it in
    two ticks.
    """
    if task.packet is None:
        return []
    target = task.packet.get("to")
    if not target:
        # A packet the handoff schema would have rejected. Escalating names it;
        # an unguarded task.packet["to"] would raise KeyError out of the pure
        # core instead, taking the whole tick down with it.
        return [Escalate(task=task.number, reason="packet_without_target")]
    if target not in labels_module.STATES:
        # The same fact, one step further in: a target that is present but names
        # no state this repo has a label for. `expand` refuses it with ValueError
        # (expand.py:91), and that raises out of the whole tick rather than out
        # of one task -- every other task's intents are discarded with it. The
        # vocabulary is checked against labels.STATES precisely because that is
        # the set `expand` can write, so decide() never emits an Advance the
        # effect layer cannot express.
        return [Escalate(task=task.number, reason="packet_target_unknown")]
    return [Advance(task=task.number, to=target)]


def _stalled(
    task: Task, observations: dict[int, Observation], policy: Policy, now: datetime
) -> bool:
    observation = observations.get(task.number)
    if observation is None:
        return False
    return now - observation.since >= timedelta(minutes=policy.stall_minutes)


def _reap(
    ledger: list[Task],
    policy: Policy,
    agents: list[AgentState],
    observations: dict[int, Observation],
    now: datetime,
) -> list[Intent]:
    """Status first, cost second, per spec §5's table read in order."""
    by_task = {a.task: a for a in agents}
    intents: list[Intent] = []
    for task in ledger:
        if task.state not in _LIVE:
            continue
        agent = by_task.get(task.number)
        if task.state != "claimed":
            # The agent moved the label itself -- `fleet handoff` for a review,
            # `fleet verdict` for an integration -- and then exited. Nothing to
            # advance and nothing to release. Retire it so the slot, the agent
            # and the worktree come back.
            #
            # `agent is None` is NOT drift here: the one-tick gap between
            # retiring one stage's agent and dispatching the next is ordinary,
            # and reading it as drift would release a task that is merely
            # waiting its turn.
            if agent is None:
                continue
            if agent.status in ("done", "dirty"):
                intents.append(
                    Retire(task=task.number)
                    if agent.status == "done"
                    else Escalate(task=task.number, reason="dirty_exit")
                )
            elif _stalled(task, observations, policy, now):
                # Not _fail: Release relabels to READY, restarting the task from
                # its implementer and discarding a review that already happened.
                intents.append(Escalate(task=task.number, reason="stalled"))
            continue
        if agent is None:
            # Ledger drift, and spec §5's table states this row unconditionally:
            # herdr's state is the tiebreaker. Escalating at the attempt cap
            # instead would burn a task to NEEDS_HUMAN for a daemon restart --
            # the one event FleetState is disposable precisely to survive.
            intents.append(Release(task=task.number, reason="no_agent", attempt=task.attempts + 1))
            continue
        if agent.status == "done":
            advanced = _advance(task)
            if advanced:
                intents += advanced
                continue
            # A done agent whose task has no packet. Waiting one tick is right --
            # `gh issue list` does not fetch comment bodies -- but waiting
            # forever is not: the task holds its slot and its area with no
            # escalation and no timer, and a clean exit whose handoff comment is
            # missing, malformed or simply not yet fetched is ordinary in
            # production. Fall through to the stall check so the wait is bounded
            # by the same timer everything else is. The cost check is skipped on
            # purpose: this agent has finished, and its work is already paid for.
            if _stalled(task, observations, policy, now):
                intents.append(_fail(task, "stalled", policy))
            continue
        if agent.status == "dirty":
            intents.append(_fail(task, "dirty_exit", policy))
            continue
        if _stalled(task, observations, policy, now):
            intents.append(_fail(task, "stalled", policy))
            continue
        # Cost is checked only for a live agent. A finished agent a cent over
        # budget still advances its packet rather than discarding work already
        # paid for.
        if agent.cost_usd > policy.per_task_usd:
            intents.append(_fail(task, "over_budget", policy))
    return intents


def _unblock(ledger: list[Task]) -> list[Intent]:
    """BLOCKED tasks whose dependency reached DONE. No human is involved -- that
    is the entire reason `block` is kept distinct from `escalate`."""
    done = {t.number for t in ledger if t.state == "done"}
    return [
        Unblock(task=t.number)
        for t in ledger
        # A dependency we cannot see stays blocked. Treating an unrecorded or
        # absent `blocked_on` as satisfied requeues work whose prerequisite
        # never landed.
        if t.state == "blocked" and t.blocked_on is not None and t.blocked_on in done
    ]


def _unroutable(ledger: list[Task], policy: Policy) -> list[Intent]:
    """READY, auto-ok tasks the policy cannot route. A human is the only fix.

    Not dispatching them is right -- `route[0]` on an empty route is an
    IndexError out of the pure core. Saying nothing about them is not: the task
    was skipped silently on every tick, with no intent, no runlog line and
    `fleet plan` printing "no action", so a missing `class:` label or a class
    absent from task-classes.md parked real work in READY for the whole night
    with nothing able to notice.

    Its own phase rather than a branch in `_dispatch`, so the escalation is not
    gated on a free slot or a free area: it is a fact about the policy, not about
    capacity, and a full fleet would otherwise hide the misconfiguration for
    exactly as long as it stayed busy.

    `auto_ok` is required. Its absence is a human's decision not to run this task
    unattended, which is why it is sitting in READY -- escalating that is noise.
    """
    return [
        Escalate(task=task.number, reason="unroutable")
        for task in ledger
        if task.state == "ready" and task.auto_ok and not policy.route_for(task.klass)
    ]


def _dispatch(
    ledger: list[Task], policy: Policy, agents: list[AgentState], spent_usd: float
) -> list[Intent]:
    committed = spent_usd + sum(a.cost_usd for a in agents)
    # No `fleet_usd > 0` guard: that turned a misconfigured ceiling into no
    # ceiling at all, which is the wrong direction for a rule whose purpose is a
    # spending bound. `load_config` refuses a non-positive budget instead, so
    # this comparison has one meaning.
    if committed >= policy.fleet_usd * _DRAIN_FRACTION:
        # Drain, do not kill: in-flight tasks finish. Killing at the ceiling
        # throws away work already paid for, and the tokens are spent either way.
        return [Drain(reason="budget")]

    live = [t for t in ledger if t.state in _LIVE]
    if any(t.klass == "integration" for t in live):
        # Global exclusive lock. The integrator touches every area, so per-area
        # exclusion does not cover it (harness amendment 2).
        return []

    held = {t.area for t in live if t.area}
    # A running agent occupies a slot whatever its row says. Counting off live
    # LABELS alone lets the two sources of truth disagree -- REAP and the drain
    # read `agents`, this read the ledger -- and drift in that gap pushed the
    # fleet past its own ceiling AND started a second agent on a task that
    # already had one, in the same worktree.
    busy = {a.task for a in agents}
    free = policy.slots - len({t.number for t in live} | busy)
    # An integration task dispatched by THIS tick arms the lock for the rest of
    # it. The check above only sees tasks that were already live, so two READY
    # integration tasks armed nothing and both went out together -- two
    # integrators rebasing onto the integration branch at once, which is the
    # collision the lock exists to prevent. Tracked like `held` rather than
    # short-circuiting the loop: the lock is one integrator at a time, not one
    # dispatch at a time, and tick 1 of integration-lock.yaml pins that an
    # integrator and ordinary tasks may still start together.
    integrating = False
    intents: list[Intent] = []
    for task in sorted(ledger, key=lambda t: t.number):
        if free <= 0:
            break
        if task.state != "ready" or not task.auto_ok:
            continue
        if task.number in busy:
            continue
        route = policy.route_for(task.klass)
        if not route:
            continue
        if task.klass == "integration" and integrating:
            continue
        if task.area is not None and task.area in held:
            continue
        intents.append(
            Dispatch(
                task=task.number,
                role=route[0],
                base=policy.integration_branch,
                # The attempt already on the task, not the next one: a dispatch
                # does not consume an attempt, a release does. One number,
                # computed here, written by expand().
                attempt=max(task.attempts, 1),
            )
        )
        if task.area is not None:
            held.add(task.area)
        if task.klass == "integration":
            integrating = True
        free -= 1
    return intents


def _stopped(intents: list[Intent]) -> set[int]:
    """The tasks whose agent this tick ends. One definition, two readers: the
    cost that becomes final and the observation that stops describing anything."""
    return {i.task for i in intents if isinstance(i, _REAPING)}


def _final_cost(intents: list[Intent], agents: list[AgentState]) -> float:
    """Only a reaped agent's cost is final, so only a reaped agent's cost lands
    in the running total. `AgentState.cost_usd` is cumulative per agent: adding
    every live agent every tick re-adds the same dollars, and six agents at
    $0.50 drain a $25 fleet in seven ticks of a night that spent three."""
    return sum(a.cost_usd for a in agents if a.task in _stopped(intents))


def decide(
    prev: FleetState,
    ledger: list[Task],
    policy: Policy,
    agents: list[AgentState],
    now: datetime,
) -> tuple[list[Intent], FleetState]:
    """A closed issue is not a task. See `_open`."""
    observations = _observe(prev, agents, now)
    open_ledger = _open(ledger)
    intents: list[Intent] = []
    intents += _reap(open_ledger, policy, agents, observations, now)
    intents += _unblock(open_ledger)
    intents += _unroutable(open_ledger, policy)
    intents += _dispatch(open_ledger, policy, agents, prev.spent_usd)
    # An observation describes one agent's sequence, so it dies with that agent:
    # carried forward past the reap, the task's NEXT agent inherits a `since`
    # from the dead one -- a stall timer already running before the replacement
    # has done anything at all.
    #
    # Intersected with the agents actually seen this tick, not applied to every
    # reaping intent. A `no_agent` release is emitted precisely when the snapshot
    # shows no agent, and pruning on it would discard the timer of an agent that
    # merely flickered -- the carry-forward in `_observe` exists to survive that
    # exact gap, and dropping the entry here would undo it one line later.
    stopped = _stopped(intents) & {a.task for a in agents}
    state = FleetState(
        seen=tuple(sorted((t, o) for t, o in observations.items() if t not in stopped)),
        spent_usd=prev.spent_usd + _final_cost(intents, agents),
    )
    return intents, state
