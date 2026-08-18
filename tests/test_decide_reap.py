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
    Retire,
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
    max_rebases=1,
    per_task_usd=2.0,
    fleet_usd=25.0,
)


def claimed(number=1, attempts=1, state="claimed", **kw):
    return Task(
        number=number,
        title="t",
        state=state,
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


def test_a_done_agent_whose_packet_never_arrives_is_released_by_the_stall_timer():
    """Waiting one tick for a packet is correct; waiting forever wedges the task.
    Catches the `continue` that skipped the stall check for a done agent, under
    which a clean exit whose handoff comment is missing, malformed or not yet
    fetched held its slot and its area for the rest of the night with nothing
    able to release it. FakeExecutor cannot produce this shape, so no scenario
    reaches it -- in production it is ordinary."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=10))
    assert Release(task=1, reason="stalled", attempt=2) in intents


def test_a_done_agent_whose_packet_has_not_arrived_yet_is_left_alone():
    """The other side of the same rule: one tick of waiting costs nothing and
    releasing here would burn an attempt on a healthy task."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=9))
    assert intents == []


def test_a_packet_with_no_target_escalates_rather_than_raising():
    """Catches `task.packet["to"]`, an unguarded index that raises KeyError out
    of the pure core and takes the whole tick with it."""
    task = claimed(packet={"head_sha": "a91c204"})
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Escalate(task=1, reason="packet_without_target") in intents


def test_a_packet_naming_a_state_that_does_not_exist_escalates_rather_than_raising():
    """The sibling of the test above, for a target that is present but wrong.
    decide() guards the MISSING key and hands the bad VALUE straight through as
    Advance(to=...), which expand() then refuses with ValueError -- out of the
    tick, taking every other task's intents with it. A packet arrives from an
    issue comment, so its `to` is exactly as trustworthy as the absent one."""
    task = claimed(packet={"to": "shipped", "head_sha": "a91c204"})
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Escalate(task=1, reason="packet_target_unknown") in intents
    assert only(intents, Advance) == []


def test_every_advance_decide_emits_can_be_expanded():
    """Binds the two halves: whatever `to` values decide() is willing to emit,
    expand() must be able to write. Catches the pair drifting apart again."""
    from fleet.expand import expand

    for target in ("review", "integrating", "done", "blocked", "human", "claimed"):
        task = claimed(packet={"to": target, "head_sha": "a91c204"})
        agents = [AgentState(task=1, status="done", change_seq=10)]
        intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
        for intent in intents:
            expand(intent, POLICY)


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


def test_an_agent_exactly_at_the_per_task_ceiling_is_not_released():
    """The boundary, pinned the way the stall boundary already is. Catches `>`
    flipped to `>=`, which releases an agent that has spent exactly its budget
    and not a cent more -- burning an attempt for arithmetic."""
    agents = [AgentState(task=1, status="working", change_seq=10, cost_usd=POLICY.per_task_usd)]
    intents, _ = decide(FleetState(), [claimed()], POLICY, agents, T0)
    assert only(intents, Release) == []


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


def test_a_retired_agent_s_cost_is_final():
    """Cost is finalised by _REAPING membership. Left out, a whole stage's spend
    -- every implementer that ever handed off -- is silently never counted, and
    the fleet ceiling is short by that much for the entire night."""
    from fleet.decide import _final_cost
    from fleet.model import Retire

    agents = [AgentState(task=5, status="done", change_seq=9, cost_usd=1.25)]
    assert _final_cost([Retire(task=5)], agents) == 1.25


def test_reaping_a_task_drops_the_observation_it_no_longer_describes():
    """An Observation describes one agent's sequence. Once that agent is reaped
    the entry is about a process that no longer exists, and it was carried
    forward for the rest of the night: `seen` only ever grew, and the stall timer
    of the task's NEXT agent started from the dead one's `since` unless the
    replacement's sequence happened to differ."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="dirty", change_seq=10)]
    _, state = decide(prev, [claimed()], POLICY, agents, T0)
    assert state.observation(1) is None


def test_escalating_a_task_drops_its_observation_too():
    """Every reaping intent ends the agent, so every one of them ends the
    observation. Catches a prune that handles Release alone."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="dirty", change_seq=10)]
    intents, state = decide(prev, [claimed(attempts=2)], POLICY, agents, T0)
    assert Escalate(task=1, reason="attempts_exhausted") in intents
    assert state.observation(1) is None


def test_advancing_a_task_drops_its_observation():
    task = claimed(packet={"to": "review", "head_sha": "a91c204"})
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="done", change_seq=10)]
    _, state = decide(prev, [task], POLICY, agents, T0)
    assert state.observation(1) is None


def test_a_drift_release_keeps_the_observation_the_snapshot_merely_lost():
    """The boundary of the prune. `no_agent` is emitted exactly when the snapshot
    shows no agent, which is also what a herdr query racing a pane restart looks
    like -- so pruning on every reaping intent would discard the idle clock of an
    agent that only flickered, undoing `_observe`'s carry-forward one line later.
    Catches a prune written as `_stopped(intents)` with no intersection."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    intents, state = decide(prev, [claimed()], POLICY, [], T0 + timedelta(minutes=5))
    assert Release(task=1, reason="no_agent", attempt=2) in intents
    assert state.observation(1) == Observation(change_seq=10, since=T0)


def test_a_live_agents_observation_survives_the_tick():
    """The other half: pruning must not eat the timer of an agent that is still
    running, or nothing is ever detected as stalled again."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="working", change_seq=11)]
    _, state = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=1))
    assert state.observation(1) == Observation(change_seq=11, since=T0 + timedelta(minutes=1))


def test_a_replacement_agent_starts_its_stall_timer_from_scratch():
    """The consequence worth having. Release, re-dispatch, and the new agent's
    first observation is its own -- so a replacement cannot inherit a `since`
    that is already older than the stall threshold and be released on sight."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    _, released = decide(prev, [claimed()], POLICY, [AgentState(1, "dirty", 10)], T0)
    fresh = T0 + timedelta(minutes=30)
    replacement = [AgentState(task=1, status="working", change_seq=10)]
    intents, state = decide(released, [claimed(attempts=2)], POLICY, replacement, fresh)
    assert state.observation(1) == Observation(change_seq=10, since=fresh)
    assert only(intents, Release) == []
    assert only(intents, Escalate) == []


def test_state_is_derived_and_carries_no_authority():
    """FleetState holds observations and spend, nothing else. Anything that
    looks like task state or ownership belongs in the ledger."""
    _, state = decide(FleetState(), [claimed()], POLICY, [], T0)
    assert set(vars(state)) == {"seen", "spent_usd"}


def test_a_finished_agent_on_a_reviewing_task_is_retired():
    """The implementer ran `fleet handoff --to review`, which moved the label
    itself, then exited. REAP used to skip every non-CLAIMED row, so this agent
    was never stopped: it stayed in the snapshot, kept _dispatch's `busy` set
    counting it, and the task could never reach a reviewer."""
    task = claimed(number=5, state="review")
    agents = [AgentState(task=5, status="done", change_seq=3, cost_usd=0.5)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Retire(task=5) in intents
    assert not any(isinstance(i, (Release, Advance)) for i in intents)


def test_a_working_agent_on_a_reviewing_task_is_left_alone():
    """A reviewer mid-review is not a leak. Retiring on presence rather than on
    an exited status would kill every reviewer one tick after it started."""
    task = claimed(number=5, state="review")
    agents = [AgentState(task=5, status="working", change_seq=3)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert intents == []


def test_a_stalled_reviewer_escalates_rather_than_releasing():
    """_fail relabels to READY, which would restart the task from its
    implementer and throw away a review that already happened. Escalate is
    bounded and terminal; a stage that failed after the work was done is a
    human's problem."""
    task = claimed(number=5, state="review")
    first = [AgentState(task=5, status="working", change_seq=3)]
    _, state = decide(FleetState(), [task], POLICY, first, T0)
    later = T0 + timedelta(minutes=POLICY.stall_minutes + 1)
    intents, _ = decide(state, [task], POLICY, first, later)
    assert intents == [Escalate(task=5, reason="stalled")]


def test_a_dirty_integrator_escalates():
    task = claimed(number=5, state="integrating")
    agents = [AgentState(task=5, status="dirty", change_seq=3)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert intents == [Escalate(task=5, reason="dirty_exit")]


def test_a_reviewing_task_with_no_agent_is_left_alone():
    """The gap between the implementer being retired and the reviewer starting
    is one tick wide and entirely normal. Reading it as drift would release a
    task that is waiting to be dispatched, burning an attempt for nothing."""
    task = claimed(number=5, state="review")
    intents, _ = decide(FleetState(), [task], POLICY, [], T0)
    assert not any(isinstance(i, (Release, Escalate, Retire)) for i in intents)
