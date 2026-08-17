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

from fleet.model import (
    Advance,
    AgentState,
    Escalate,
    FleetState,
    Intent,
    Observation,
    Policy,
    Release,
    Task,
    Unblock,
)

# Intents whose expansion stops the agent, so that agent's cost is final.
_REAPING = (Advance, Release, Escalate)


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
        if task.state != "claimed":
            continue
        agent = by_task.get(task.number)
        if agent is None:
            # Ledger drift, and spec §5's table states this row unconditionally:
            # herdr's state is the tiebreaker. Escalating at the attempt cap
            # instead would burn a task to NEEDS_HUMAN for a daemon restart --
            # the one event FleetState is disposable precisely to survive.
            intents.append(Release(task=task.number, reason="no_agent", attempt=task.attempts + 1))
            continue
        if agent.status == "done":
            intents += _advance(task)
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


def _final_cost(intents: list[Intent], agents: list[AgentState]) -> float:
    """Only a reaped agent's cost is final, so only a reaped agent's cost lands
    in the running total. `AgentState.cost_usd` is cumulative per agent: adding
    every live agent every tick re-adds the same dollars, and six agents at
    $0.50 drain a $25 fleet in seven ticks of a night that spent three."""
    stopped = {i.task for i in intents if isinstance(i, _REAPING)}
    return sum(a.cost_usd for a in agents if a.task in stopped)


def decide(
    prev: FleetState,
    ledger: list[Task],
    policy: Policy,
    agents: list[AgentState],
    now: datetime,
) -> tuple[list[Intent], FleetState]:
    observations = _observe(prev, agents, now)
    intents: list[Intent] = []
    intents += _reap(ledger, policy, agents, observations, now)
    intents += _unblock(ledger)
    state = FleetState(
        seen=tuple(sorted(observations.items())),
        spent_usd=prev.spent_usd + _final_cost(intents, agents),
    )
    return intents, state
