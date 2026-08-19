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
    Close,
    Continue,
    Dispatch,
    Drain,
    Escalate,
    FleetState,
    Intent,
    Observation,
    Policy,
    Rebase,
    Release,
    Retire,
    Task,
    Unblock,
)

# Intents whose expansion stops the agent, so that agent's cost is final.
#
# Public because the daemon reads it to decide which log entries carry a
# finalised cost, and a private name copied into a second module is the shape
# that lets the two definitions drift -- at which point the run log and
# FleetState.spent_usd disagree, and a restart resumes on a different budget
# from the one it stopped with.
REAPING = (Advance, Release, Rebase, Escalate, Retire, Close)

_DRAIN_FRACTION = 0.8

# States that hold an area and a slot. REVIEW is live because someone is working
# on the task: it keeps its area until `fleet verdict` moves it out, which is the
# only legal exit (spec §5 ruling 4).
_LIVE = ("claimed", "review", "integrating")

# Which role a task's state is owed, as an index into its class's route.
# `integrating` is route[-1] rather than route[2] because M4 adds the invariant
# reviewer and a four-role patch route still ends at the integrator. M4 edits
# this table regardless -- an invariant-review stage needs a state of its own --
# and three explicit rows are a better thing to edit than a positional guess.
#
# `claimed` maps to the same index as `ready`, route[0]: CLAIMED is what READY
# becomes the instant `_dispatch` hands it to route[0], the same role, just
# after the handoff rather than before it. Added for `_model_mismatch`, the
# first caller that needs a CLAIMED task's role -- every earlier reader of this
# table only ever asked about a task about to be dispatched or continued, and a
# live-but-not-yet-advanced CLAIMED row is neither, so nothing needed the row
# until now.
_STAGE = {"ready": 0, "claimed": 0, "review": 1, "integrating": -1}

# The states that are already holding a slot and an area: their own.
_CONTINUATIONS = ("review", "integrating")


def _role_for(task: Task, policy: Policy) -> str | None:
    """The role this task's state is owed, or None when the policy has none.

    None is never a crash and never a silent skip: `_unroutable` turns it into
    an Escalate naming which of the two policy errors it is.
    """
    route = policy.route_for(task.klass)
    if not route:
        return None
    index = _STAGE[task.state]
    if index >= len(route):
        return None
    if task.state == "integrating" and len(route) < 2:
        # route[-1] on a one-role route resolves to route[0], which would send a
        # `spec` task's planner to merge the integration branch. A route that
        # cannot produce an INTEGRATING state cannot legitimately arrive here.
        return None
    return route[index]


def _model_mismatch(task: Task, agent: AgentState, policy: Policy) -> bool:
    """True when an agent is demonstrably running a model nobody declared.

    `UnpricedModel` catches a model with no price at all; it cannot catch this
    -- a substitution between two PRICED models raises nothing and bills the
    night at a rate nobody chose, the exact hazard `AgentState.model`'s own
    docstring names.

    False whenever the answer is not yet known: `model=None` is a transcript
    that has not been written, ordinary in the seconds after a dispatch, and
    escalating on it would burn the backlog on the first tick. `MULTIPLE_MODELS`
    is not that -- it is `parse_snapshot` naming a transcript that already
    carries two models, which is the substitution itself, so it must compare
    unequal to whatever a policy could ever declare rather than read as unknown.
    """
    if agent.model is None:
        return False
    role = _role_for(task, policy)
    if role is None:
        return False
    declared = policy.agent_for(role)
    if declared is None:
        return False
    return agent.model != declared[1]


def _writes_integration(task: Task) -> bool:
    """Both halves of the global exclusive lock.

    `klass == "integration"` was complete only while nothing could dispatch an
    integrator any other way. A class:patch task at INTEGRATING has one writing
    the same branch, and two of those at once is exactly the collision the lock
    exists to prevent.
    """
    return task.klass == "integration" or task.state == "integrating"


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

    Three phases, three different answers about a closed row, so this is no
    longer applied to all of them: DISPATCH and `_unroutable` take this filtered
    list, UNBLOCK takes it for candidates but computes `done` over every row, and
    REAP takes the unfiltered ledger. See `decide` for why each is what it is.
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

    A packet from a DIFFERENT attempt is treated the same way as no packet:
    ignored, and the caller falls through to the stall timer. The issue outlives
    the attempt, so the newest handoff comment can be attempt 1's evidence read
    on attempt 2 -- and advancing on it moves a task forward on work that was
    thrown away. A missing `attempt` is a mismatch, not a pass: a permissive
    default here makes the whole check decorative.
    """
    if task.packet is None:
        return []
    if task.packet.get("attempt") != max(task.attempts, 1):
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


def _is_rebase_handback(task: Task) -> bool:
    """An integrator handing a task back to CLAIMED for a rebase.

    Three halves, now. The target alone would let any role dodge an attempt by
    naming CLAIMED; the role alone would catch an integrator's ordinary hand to
    `done`; and neither says WHEN the handback was written.

    The attempt binding is spec §5.5, the same rule `_advance` applies and for
    the same reason. `packet.parse_latest` returns the newest handoff comment
    across the whole issue's history with no per-attempt scoping, and the issue
    outlives the attempt -- so attempt 1's handback is still "latest" while the
    task is on attempt 2. This branch runs ahead of every other one in `_reap`,
    so without the binding a stale packet charged the task a rebase it never
    made, hit `max_rebases`, and sent it to NEEDS_HUMAN naming
    `rebases_exhausted` for work that never happened. A missing `attempt` is a
    mismatch, not a pass: `handoff.schema.json` requires the field, and a
    permissive default here makes the whole check decorative.
    """
    packet = task.packet or {}
    if packet.get("attempt") != max(task.attempts, 1):
        return False
    sender = packet.get("from") or {}
    return packet.get("to") == "claimed" and sender.get("role") == "integrator"


def _reap(
    ledger: list[Task],
    policy: Policy,
    agents: list[AgentState],
    observations: dict[int, Observation],
    now: datetime,
    dispatched: dict[int, str],
) -> list[Intent]:
    """Status first, cost second, per spec §5's table read in order.

    `dispatched` is the previous tick's `FleetState.dispatched` as a mapping:
    task -> the state that task's agent was CONTINUED in. `AgentState` carries no
    role, so a reviewer that exited without writing a verdict and an implementer
    that handed off to REVIEW are the same snapshot -- a done agent on a REVIEW
    row. The allocator started one of them, and this is the record of that.
    """
    by_task = {a.task: a for a in agents}
    intents: list[Intent] = []
    for task in ledger:
        if not task.is_open:
            # The issue was closed under the fleet -- by a human, or by our own
            # Advance-to-done on a previous tick. Whatever the label says, the
            # work is over. Stop the agent if there still is one; do not
            # relabel, because a closed row put back to fleet:ready advertises
            # itself as available work that nothing will ever pick up.
            if task.number in by_task:
                intents.append(Retire(task=task.number))
            continue
        if task.state == "done":
            # OPEN, labelled done, and an agent that put it there. Spec §6 makes
            # reaching DONE close the issue and `expand(Advance(to="done"))`
            # does -- but nothing in the real shell emits that Advance. An
            # integrator reaches DONE by running `fleet handoff --to done`, and
            # `commands._post` moves the label itself, so this tick reads a row
            # already at `fleet:done`. That state is not in `_LIVE`, so the
            # branch below skipped it before `_advance` was ever reachable, and
            # the orphan pass emitted `Retire` -- which deliberately carries no
            # relabel and no close. The row stayed OPEN for the rest of the
            # night: spec §6 unmet, and `_unblock`'s closed-dependency rule with
            # nothing to read.
            #
            # Gated on an agent, and that gate is the rule, not an optimisation.
            # An OPEN row at `fleet:done` with no agent is not the fleet's work
            # finishing; it is a row somebody else labelled -- a dependency
            # recorded as complete by hand, which is exactly
            # blocked-self-resolves.yaml's issue 11. Closing those would make
            # the allocator close issues it never ran, on the strength of a
            # label a human wrote. The residual cost is stated rather than
            # hidden: if the daemon is down across the one tick between the
            # label move and the close AND herdr loses the agent in the same
            # window, that row stays OPEN and a human closes it. One narrow
            # window, against closing every hand-labelled row on the ledger.
            #
            # Idempotent by ORDER rather than by repetition, which is why
            # `expand(Close)` closes before it stops. A close that does not land
            # leaves the row OPEN with its agent still live, so the next tick
            # arrives here again and retries. A close that lands but whose stop
            # fails leaves a CLOSED row with an agent, which the closed-row rule
            # directly above already retires. Neither failure is terminal and
            # neither needs memory.
            #
            # Ahead of the `_LIVE` check and below the closed-row rule, which is
            # what bounds it: once the close lands, `is_open` goes False and the
            # row falls to that rule -- which, with its agent gone, produces
            # nothing at all, every tick, forever.
            #
            # One intent, not two: this task is also, verbatim, the orphan
            # condition -- an agent on a row that is not live -- and the orphan
            # pass's `handled` set is what keeps it from drawing a Retire too.
            if task.number in by_task:
                intents.append(Close(task=task.number))
            continue
        if task.state not in _LIVE:
            continue
        agent = by_task.get(task.number)
        if (agent is None or agent.status == "done") and _is_rebase_handback(task):
            # An integrator handing this task back for a rebase, checked ahead
            # of every other branch in this loop -- Ruling B3.
            #
            # `_post` writes the handback comment and only then moves the
            # label (commands.py `_post`), so a tick can catch this row
            # mid-transition: the label still reads INTEGRATING while the
            # packet is already visible. Left to the branches below, that row
            # falls into the non-claimed body and its done-agent case emits
            # Retire -- an ordinary stage-handoff reading of what is actually
            # a rebase.
            #
            # And once the label has caught up to CLAIMED, the integrator that
            # wrote it still shows `status="done"` in herdr's snapshot for one
            # more tick, until StopAgent removes it. Left to the branches
            # below, that reads as a clean exit and `_advance` emits a
            # meaningless Advance(task, to="claimed") self-transition instead.
            #
            # Neither shape is a failure and neither is ledger drift, so this
            # precedes both the Retire branch and `_advance`.
            #
            # Gated on `agent is None or agent.status == "done"` and NOT on
            # the packet alone: `packet.parse_latest` returns the newest
            # handoff comment across the whole issue's history with no
            # per-attempt scoping (spec §5.5; Task 7 binds a packet to its
            # attempt, not this task). After a legitimate rebase and
            # re-dispatch, the integrator's original handback stays "latest"
            # until the new implementer posts its own -- which can be many
            # ticks. An unconditional check reads that stale packet out from
            # under a genuinely working (or dirty) agent and re-fires the
            # rebase/escalate decision on every one of those ticks, which at
            # `max_rebases=1` auto-escalates a task to NEEDS_HUMAN one tick
            # after a rebase that just succeeded -- the exact loop this
            # branch exists to bound, inverted. NEVER widen this to consult a
            # packet while an agent is actively working: a stale packet is a
            # fact about issue-comment history, not about what the current
            # agent is doing right now.
            if task.rebases + 1 > policy.max_rebases:
                intents.append(Escalate(task=task.number, reason="rebases_exhausted"))
            else:
                intents.append(Rebase(task=task.number, rebase=task.rebases + 1))
            continue
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
            if agent.status == "done":
                if dispatched.get(task.number) == task.state:
                    # We started this agent FOR this state and the state has not
                    # moved, so the stage produced nothing -- a reviewer that
                    # wrote no verdict, an integrator that merged nothing.
                    # Escalate rather than retire: retiring starts another one,
                    # and another, until the fleet budget runs out.
                    intents.append(Escalate(task=task.number, reason="stage_produced_nothing"))
                else:
                    intents.append(Retire(task=task.number))
            elif agent.status == "dirty":
                intents.append(Escalate(task=task.number, reason="dirty_exit"))
            elif _stalled(task, observations, policy, now):
                # Not _fail: Release relabels to READY, restarting the task from
                # its implementer and discarding a review that already happened.
                intents.append(Escalate(task=task.number, reason="stalled"))
            elif agent.cost_usd > policy.per_task_usd:
                # Task 3 left this branch with no cost check at all, so a
                # reviewer that was healthy, moving and far over its ceiling got
                # no intent and ran until the whole FLEET ceiling drained.
                # Escalate for the same reason the stall above does: a Release
                # relabels to READY and throws away a review that happened.
                # Status first, cost second -- a finished stage is already paid
                # for, and the `done` branch above returns before this.
                intents.append(Escalate(task=task.number, reason="over_budget"))
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
        if _model_mismatch(task, agent, policy):
            # Ahead of the cost check below, on purpose: a wrong-model agent's
            # `cost_usd` was accumulated against the wrong rate, so an
            # over_budget verdict computed from it is not a fact worth acting
            # on. Escalate rather than `_fail`: `_fail`'s Release restarts the
            # task from its implementer on the SAME undeclared model, so the
            # replacement agent hits this exact branch again next tick. One
            # agent on the wrong model is a task fault, not a configuration
            # fault, so this stops the task rather than draining the fleet.
            intents.append(Escalate(task=task.number, reason="model_mismatch"))
            continue
        # Cost is checked only for a live agent. A finished agent a cent over
        # budget still advances its packet rather than discarding work already
        # paid for.
        if agent.cost_usd > policy.per_task_usd:
            intents.append(_fail(task, "over_budget", policy))
    # An orphaned agent: its task sits on a row in READY, DONE or BLOCKED, or
    # has no ledger row at all. The per-row loop above only ever visits rows,
    # so none of its branches can see this agent -- it holds a slot and an
    # area forever, unstoppable, because nothing else in decide() looks at
    # agents rather than rows. Walking `agents` directly is the only way to
    # catch a task number the ledger has never heard of.
    #
    # Guarded against the loop above: a closed row in a non-live state (say
    # DONE) with a working agent is retired by the closed-row rule, and that
    # same task number is also absent from every live row -- the orphan
    # condition, verbatim. Without excluding a task already producing an
    # intent this pass, that shape doubles the Retire.
    handled = {i.task for i in intents}
    live_rows = {task.number for task in ledger if task.state in _LIVE}
    for agent in agents:
        if agent.task in handled or agent.task in live_rows:
            continue
        intents.append(Retire(task=agent.task))
    return intents


def _unblock(ledger: list[Task], all_rows: list[Task]) -> list[Intent]:
    """BLOCKED tasks whose dependency reached DONE. No human is involved -- that
    is the entire reason `block` is kept distinct from `escalate`.

    `done` is computed over EVERY row, including closed ones, while the tasks
    considered for release come from the open list. A closed issue is a satisfied
    dependency whatever label it still carries: closing on completion is ordinary
    from M3 on, and `fleet:done` on a closed row is the normal end state, not an
    exception.
    """
    done = {t.number for t in all_rows if t.state == "done" or not t.is_open}
    return [
        Unblock(task=t.number)
        for t in ledger
        # A dependency we cannot see stays blocked. Treating an unrecorded or
        # absent `blocked_on` as satisfied requeues work whose prerequisite
        # never landed.
        if t.state == "blocked" and t.blocked_on is not None and t.blocked_on in done
    ]


def _unroutable(ledger: list[Task], policy: Policy) -> list[Intent]:
    """Dispatchable tasks the policy cannot route. A human is the only fix.

    Two distinct policy errors, named apart because the run log is where an
    operator finds out which one it was: `unroutable` is a task whose class has
    no route at all -- a missing `class:` label, or a class absent from
    task-classes.md. `unroutable_stage` is a task that reached a state its route
    has no role for, which is the failure M4's invariant reviewer will produce if
    it is added to a route without its state.

    Its own phase rather than a branch in `_dispatch`, so the escalation is not
    gated on a free slot or a free area: it is a fact about the policy, not about
    capacity, and a full fleet would otherwise hide the misconfiguration for
    exactly as long as it stayed busy.

    `auto_ok` is required. Its absence is a human's decision not to run this task
    unattended, which is why it is sitting there -- escalating that is noise.
    """
    out: list[Intent] = []
    for task in ledger:
        if task.state not in _STAGE or not task.auto_ok:
            continue
        if not policy.route_for(task.klass):
            out.append(Escalate(task=task.number, reason="unroutable"))
        elif _role_for(task, policy) is None:
            out.append(Escalate(task=task.number, reason="unroutable_stage"))
    return out


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
    # A running agent occupies a slot whatever its row says. Counting off live
    # LABELS alone lets the two sources of truth disagree -- REAP and the drain
    # read `agents`, this read the ledger -- and drift in that gap pushed the
    # fleet past its own ceiling AND started a second agent on a task that
    # already had one, in the same worktree.
    busy = {a.task for a in agents}
    integrators = {t.number for t in live if _writes_integration(t)}
    intents: list[Intent] = []
    # One integrator at a time, armed by whichever pass starts one. Pass 2 never
    # sees pass 1's arming -- a continued integrator is itself a live row that
    # writes integration, so `integrators` is non-empty and pass 2 returns first
    # -- but the two passes mean the same thing by it, and one flag is one rule.
    integrating = False

    # Pass 1 -- continuations. A task at REVIEW or INTEGRATING already holds its
    # area and its slot: itself. Charged either, it can never advance, and at
    # `slots` live tasks the whole fleet deadlocks with every task one stage from
    # done. Matched against OTHER live tasks only, for the same reason.
    #
    # The lock here counts integrators that are actually RUNNING plus the one
    # this tick starts, not every live INTEGRATING row: a row at INTEGRATING with
    # no agent is waiting FOR an integrator, not holding one, and excluding it
    # deadlocks two such tasks against each other -- both a stage from done,
    # neither ever started, which is the same shape as charging a continuation
    # its own slot. `task.number in busy` above already removed this task from
    # the running set, so no self-exclusion is needed.
    running_integrators = integrators & busy
    # Bounded by `slots` on the AGENTS that will be running, not on the live
    # rows. "A continuation already holds its slot" is true only while live rows
    # <= slots, and the two events M3 exists to survive break exactly that: a
    # daemon restarted against a ledger it did not build, and a human relabelling
    # rows by hand. Unbounded, ten REVIEW rows started ten agents in one tick.
    # Counting rows instead would bring back the deadlock this pass exists to
    # prevent -- at slots=1 the one reviewing task IS the one live row, so it
    # could never be advanced. Running agents plus the ones started here is the
    # count that is both bounded and non-self-excluding.
    started = 0
    for task in sorted(ledger, key=lambda t: t.number):
        if len(busy) + started >= policy.slots:
            break
        if task.state not in _CONTINUATIONS or not task.auto_ok:
            continue
        if task.number in busy:
            continue
        if _writes_integration(task) and (integrating or running_integrators):
            continue
        role = _role_for(task, policy)
        if role is None:
            continue
        # Continue, not Dispatch. Dispatch relabels to CLAIMED, which for a task
        # at REVIEW destroys the state it is being continued FOR.
        intents.append(Continue(task=task.number, role=role, base=policy.integration_branch))
        started += 1
        if _writes_integration(task):
            integrating = True

    # Pass 2 -- new work. A live integrator holds a GLOBAL lock: it touches every
    # area by definition, so per-area exclusion does not cover it. Computed from
    # tasks that were already live, not from pass 1's dispatches, because M2's
    # integration-lock.yaml pins the distinction: a live integrator blocks all
    # new dispatch, while one dispatched by this tick blocks only other
    # integrators in the same tick.
    if integrators:
        return intents

    held = {t.area for t in live if t.area}
    free = policy.slots - len({t.number for t in live} | busy)
    # An integration task dispatched by THIS tick arms the lock for the rest of
    # it. The check above only sees tasks that were already live, so two READY
    # integration tasks armed nothing and both went out together -- two
    # integrators rebasing onto the integration branch at once, which is the
    # collision the lock exists to prevent. Tracked like `held` rather than
    # short-circuiting the loop: the lock is one integrator at a time, not one
    # dispatch at a time, and tick 1 of integration-lock.yaml pins that an
    # integrator and ordinary tasks may still start together.
    for task in sorted(ledger, key=lambda t: t.number):
        if free <= 0:
            break
        if task.state != "ready" or not task.auto_ok:
            continue
        if task.number in busy:
            continue
        role = _role_for(task, policy)
        if role is None:
            continue
        if _writes_integration(task) and integrating:
            continue
        if task.area is not None and task.area in held:
            continue
        intents.append(
            Dispatch(
                task=task.number,
                role=role,
                base=policy.integration_branch,
                # The attempt already on the task, not the next one: a dispatch
                # does not consume an attempt, a release does. One number,
                # computed here, written by expand().
                attempt=max(task.attempts, 1),
            )
        )
        if task.area is not None:
            held.add(task.area)
        if _writes_integration(task):
            integrating = True
        free -= 1
    return intents


def _stopped(intents: list[Intent]) -> set[int]:
    """The tasks whose agent this tick ends. One definition, two readers: the
    cost that becomes final and the observation that stops describing anything."""
    return {i.task for i in intents if isinstance(i, REAPING)}


def charge_of(
    intents: list[Intent], agents: list[AgentState], already: dict[int, float]
) -> dict[int, float]:
    """Dollars this tick adds, per task. One definition, two readers.

    Only a reaped agent's cost is final, so only a reaped agent's cost lands in
    the running total. `AgentState.cost_usd` is cumulative per agent, so adding
    every live agent every tick re-adds the same dollars -- six agents at $0.50
    drain a $25 fleet in seven ticks of a night that spent three.

    Once per tick was not enough, and that was the hole. A reaping intent
    REPEATS: when `stop_agent` fails for a reason outside `AGENT_ABSENT_CODES`
    the executor deliberately leaves the agent alive, and the orphan pass
    re-emits `Retire` on the next tick, and the next. `Daemon._log` deduped
    within a tick; nothing deduped across them, so a $2 agent added $2 every
    tick and the run log carried the inflation into any restart.

    `already` is the high-water mark per task -- `FleetState.charged` -- so what
    is added is the INCREMENT. An agent whose stop failed is still running and
    still spending, and those dollars are real; only the ones already counted
    are not. A negative delta (a replacement agent whose cumulative cost is
    below the last one's) contributes nothing rather than a refund; the caller
    drops the mark when it starts a new agent, so that case is a snapshot
    anomaly, not a new attempt.

    Public for the same reason `REAPING` is: `Daemon._log` writes the number a
    restart reads back, and a second copy of this rule in that file is how the
    run log and `spent_usd` come to disagree.
    """
    by_task = {a.task: a for a in agents}
    out: dict[int, float] = {}
    for task in _stopped(intents):
        agent = by_task.get(task)
        if agent is None:
            continue
        delta = agent.cost_usd - already.get(task, 0.0)
        if delta > 0:
            out[task] = delta
    return out


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
    # REAP sees EVERY row. A CLOSED row still labelled fleet:claimed has a live
    # agent that nothing else can stop, and its cost counts against the ceiling
    # until something does. DISPATCH and UNBLOCK must not act on a closed row,
    # so the filter cannot stay one blanket call shared by all three phases.
    intents += _reap(ledger, policy, agents, observations, now, dict(prev.dispatched))
    intents += _unblock(open_ledger, ledger)
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
    # The continuation record dies with the agent it describes, for the same
    # reason an observation does: carried past the reap, the task's NEXT agent
    # inherits it and its first clean finish reads as "produced nothing".
    # Pruned on `_stopped` with no intersection against `agents`, unlike `seen`:
    # this record is about a stage the allocator started, and every reaping
    # intent ends that stage whether or not the snapshot still shows its agent.
    continued = {i.task for i in intents if isinstance(i, Continue)}
    stopped_now = _stopped(intents)
    carried = {t: s for t, s in prev.dispatched if t not in stopped_now and t not in continued}
    rows = {t.number: t for t in ledger}
    for task in continued:
        carried[task] = rows[task].state
    # The high-water mark per task, advanced by exactly what was charged. Then
    # dropped for every task this tick starts a new agent on -- a Dispatch or a
    # Continue means a new worktree and a new session, so that agent's
    # `cost_usd` restarts at zero and the previous mark would swallow its whole
    # spend. Popped after the charge rather than before, because the two cannot
    # collide: `_dispatch` treats any task with a live agent as busy, so a task
    # reaped this tick is never also started by it.
    charged = dict(prev.charged)
    charges = charge_of(intents, agents, charged)
    for task, amount in charges.items():
        charged[task] = charged.get(task, 0.0) + amount
    started = {i.task for i in intents if isinstance(i, (Dispatch, Continue))}
    state = FleetState(
        seen=tuple(sorted((t, o) for t, o in observations.items() if t not in stopped)),
        spent_usd=prev.spent_usd + sum(charges.values()),
        dispatched=tuple(sorted(carried.items())),
        charged=tuple(sorted((t, c) for t, c in charged.items() if t not in started)),
    )
    return intents, state
