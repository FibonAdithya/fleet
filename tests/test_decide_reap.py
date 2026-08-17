from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fleet.decide import decide
from fleet.model import (
    Advance,
    AgentState,
    Escalate,
    FleetState,
    Observation,
    Policy,
    Release,
    Task,
)

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)

POLICY = Policy(
    areas=(("src/eval/**", "eval"),),
    routes=(("patch", ("implementer", "reviewer", "integrator")),),
    integration_branch="integration",
    slots=6,
    stall_minutes=10,
    max_attempts=2,
    max_fix_passes=1,
    per_task_usd=2.0,
    fleet_usd=25.0,
)


def claimed(number=1, attempts=1, **kw):
    return Task(
        number=number,
        title="t",
        state="claimed",
        area="eval",
        klass="patch",
        auto_ok=True,
        attempts=attempts,
        **kw,
    )


def only(intents, kind):
    return [i for i in intents if isinstance(i, kind)]


def test_a_clean_exit_advances_to_the_packets_target():
    """Catches a REAP that ignores the packet and releases every finished agent."""
    task = claimed(packet={"to": "review", "head_sha": "a91c204"})
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Advance(task=1, to="review") in intents


def test_a_clean_exit_with_no_packet_waits_instead_of_failing():
    """Spec §5: `packet is None` is a fact about the query -- `gh issue list`
    does not fetch comment bodies, so the shell fetches packets for the states
    that carry one and the next tick advances. Catches the invented `no_packet`
    failure reason, which released a healthy task and escalated it in two ticks."""
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(FleetState(), [claimed()], POLICY, agents, T0)
    assert intents == []


def test_a_packet_with_no_target_escalates_rather_than_raising():
    """Catches `task.packet["to"]`, an unguarded index that raises KeyError out
    of the pure core and takes the whole tick with it."""
    task = claimed(packet={"head_sha": "a91c204"})
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Escalate(task=1, reason="packet_without_target") in intents


def test_a_dirty_exit_releases_and_names_the_next_attempt():
    """Catches a Release that carries no attempt number, which is how the
    counter ended up with two writers disagreeing about it."""
    agents = [AgentState(task=1, status="dirty", change_seq=10)]
    intents, _ = decide(FleetState(), [claimed()], POLICY, agents, T0)
    assert Release(task=1, reason="dirty_exit", attempt=2) in intents


def test_an_agent_whose_sequence_has_not_moved_for_the_threshold_is_stalled():
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="working", change_seq=10)]
    intents, _ = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=10))
    assert Release(task=1, reason="stalled", attempt=2) in intents


def test_an_agent_still_moving_is_not_stalled():
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="working", change_seq=11)]
    intents, state = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=10))
    assert only(intents, Release) == []
    assert state.observation(1).change_seq == 11
    assert state.observation(1).since == T0 + timedelta(minutes=10)


def test_an_agent_just_under_the_threshold_is_not_stalled():
    """Catches `>` flipped to `>=` on the wrong side of the comparison."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="working", change_seq=10)]
    intents, _ = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=9, seconds=59))
    assert only(intents, Release) == []


def test_an_agent_absent_for_one_tick_does_not_reset_its_stall_timer():
    """Catches an _observe that rebuilds `seen` from this tick's agents alone. A
    snapshot that momentarily omits an agent would restart its idle clock, and a
    stall that flickers would never be detected at all."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    _, mid = decide(prev, [claimed()], POLICY, [], T0 + timedelta(minutes=5))
    assert mid.observation(1) == Observation(change_seq=10, since=T0)
    agents = [AgentState(task=1, status="working", change_seq=10)]
    intents, _ = decide(mid, [claimed()], POLICY, agents, T0 + timedelta(minutes=10))
    assert Release(task=1, reason="stalled", attempt=2) in intents


def test_a_second_failure_escalates_rather_than_making_a_third_attempt():
    """Parent §4's attempt cap. Without it an unattended run spends the night
    and a large token budget re-failing one task. Catches a cap checked after
    the release rather than before it."""
    agents = [AgentState(task=1, status="dirty", change_seq=10)]
    intents, _ = decide(FleetState(), [claimed(attempts=2)], POLICY, agents, T0)
    assert Escalate(task=1, reason="attempts_exhausted") in intents
    assert only(intents, Release) == []


def test_a_claimed_task_with_no_live_agent_is_released_even_at_the_attempt_cap():
    """Ledger drift; spec §5 states this row unconditionally. Catches applying
    the attempt cap to it, which burns a task to NEEDS_HUMAN for a daemon
    restart -- the one event FleetState is disposable in order to survive."""
    intents, _ = decide(FleetState(), [claimed(attempts=2)], POLICY, [], T0)
    assert Release(task=1, reason="no_agent", attempt=3) in intents
    assert only(intents, Escalate) == []


def test_over_per_task_budget_releases():
    agents = [AgentState(task=1, status="working", change_seq=10, cost_usd=2.5)]
    intents, _ = decide(FleetState(), [claimed()], POLICY, agents, T0)
    assert Release(task=1, reason="over_budget", attempt=2) in intents


def test_a_finished_agent_over_budget_still_advances_its_packet():
    """Status first, cost second. Catches the reverse order, which discards a
    completed packet because the agent ended a cent over the ceiling -- paying
    for the work and then throwing it away."""
    task = claimed(packet={"to": "review", "head_sha": "a91c204"})
    agents = [AgentState(task=1, status="done", change_seq=10, cost_usd=2.5)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Advance(task=1, to="review") in intents
    assert only(intents, Release) == []


def test_a_closed_task_is_not_reaped():
    """A CLOSED issue is not a task, whatever its labels still say. Releasing it
    would write labels onto a closed issue and hand the work out again."""
    intents, _ = decide(FleetState(), [claimed(is_open=False)], POLICY, [], T0)
    assert intents == []


def test_a_task_that_is_not_claimed_is_not_reaped():
    ready = Task(number=1, title="t", state="ready", area="eval", klass="patch", auto_ok=True)
    intents, _ = decide(FleetState(), [ready], POLICY, [], T0)
    assert only(intents, Release) == []


def test_only_a_reaped_agents_cost_reaches_the_running_total():
    """`AgentState.cost_usd` is cumulative per agent, so re-adding every live
    agent every tick re-adds the same dollars. Catches exactly that: six agents
    at $0.50 would drain a $25 fleet in seven ticks of a night that spent $3."""
    agents = [AgentState(task=1, status="working", change_seq=10, cost_usd=0.5)]
    _, state = decide(FleetState(), [claimed()], POLICY, agents, T0)
    assert state.spent_usd == 0.0


def test_a_reaped_agents_final_cost_reaches_the_running_total():
    """Catches the opposite mutation -- never accumulating at all, so the fleet
    budget is never spent and the drain never fires."""
    agents = [AgentState(task=1, status="dirty", change_seq=10, cost_usd=0.5)]
    _, state = decide(FleetState(spent_usd=1.0), [claimed()], POLICY, agents, T0)
    assert state.spent_usd == 1.5


def test_state_is_derived_and_carries_no_authority():
    """FleetState holds observations and spend, nothing else. Anything that
    looks like task state or ownership belongs in the ledger."""
    _, state = decide(FleetState(), [claimed()], POLICY, [], T0)
    assert set(vars(state)) == {"seen", "spent_usd"}
