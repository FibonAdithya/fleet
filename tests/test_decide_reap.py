from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from fleet.decide import _advance, decide
from fleet.model import (
    Advance,
    AgentState,
    Close,
    Dispatch,
    Escalate,
    FleetState,
    Observation,
    Policy,
    Rebase,
    Release,
    Retire,
    Task,
)

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)

POLICY = Policy(
    areas=(("src/eval/**", "eval"),),
    routes=(("patch", ("implementer", "reviewer", "integrator")),),
    agents=(),
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
    task = claimed(packet={"to": "review", "attempt": 1, "head_sha": "a91c204"})
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
    task = claimed(packet={"attempt": 1, "head_sha": "a91c204"})
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Escalate(task=1, reason="packet_without_target") in intents


def test_a_packet_naming_a_state_that_does_not_exist_escalates_rather_than_raising():
    """The sibling of the test above, for a target that is present but wrong.
    decide() guards the MISSING key and hands the bad VALUE straight through as
    Advance(to=...), which expand() then refuses with ValueError -- out of the
    tick, taking every other task's intents with it. A packet arrives from an
    issue comment, so its `to` is exactly as trustworthy as the absent one."""
    task = claimed(packet={"to": "shipped", "attempt": 1, "head_sha": "a91c204"})
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Escalate(task=1, reason="packet_target_unknown") in intents
    assert only(intents, Advance) == []


def test_every_advance_decide_emits_can_be_expanded():
    """Binds the two halves: whatever `to` values decide() is willing to emit,
    expand() must be able to write. Catches the pair drifting apart again."""
    from fleet.expand import expand

    for target in ("review", "integrating", "done", "blocked", "human", "claimed"):
        task = claimed(packet={"to": target, "attempt": 1, "head_sha": "a91c204"})
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


def test_answered_task_gets_a_third_attempt():
    """An owner answer raises the attempt cap to max_attempts * (1 + answers).
    Catches `_fail` still comparing against the bare policy cap, which escalates
    an answered task straight back to the owner without trying the answer."""
    agents = [AgentState(task=1, status="dirty", change_seq=10)]
    task = claimed(attempts=2, answers=1)
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Release(task=1, reason="dirty_exit", attempt=3) in intents
    assert only(intents, Escalate) == []


def test_answered_task_attempt_at_new_cap_is_released():
    """The boundary of the raised cap: attempt 4 of 4 is allowed. Catches `>=`
    for `>`, and `max_attempts + answers` (cap 3) for `max_attempts * (1 + answers)`."""
    agents = [AgentState(task=1, status="dirty", change_seq=10)]
    task = claimed(attempts=3, answers=1)
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Release(task=1, reason="dirty_exit", attempt=4) in intents
    assert only(intents, Escalate) == []


def test_answered_task_escalates_after_its_new_allowance():
    """The raised cap still binds. Catches an answer that lifts the cap entirely."""
    agents = [AgentState(task=1, status="dirty", change_seq=10)]
    task = claimed(attempts=4, answers=1)
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Escalate(task=1, reason="attempts_exhausted") in intents
    assert only(intents, Release) == []


def test_old_attempt_packet_does_not_advance_after_answer():
    """Regression guard: a packet is bound to the task's current attempt, not to
    the cap. An answered task on attempt 3 must not advance on attempt 1's
    packet. Catches a change that compares the packet's attempt to the cap."""
    stale = {"to": "review", "attempt": 1, "from": {"role": "implementer"}}
    task = claimed(attempts=3, answers=1, packet=stale)
    assert _advance(task) == []
    fresh = {"to": "review", "attempt": 3, "from": {"role": "implementer"}}
    current = claimed(attempts=3, answers=1, packet=fresh)
    assert _advance(current) == [Advance(task=1, to="review")]


def test_a_claimed_task_with_no_live_agent_is_released_even_at_the_attempt_cap():
    """Ledger drift; spec §5 states this row unconditionally. Catches applying
    the attempt cap to it, which burns a task to NEEDS_HUMAN for a daemon
    restart -- the one event FleetState is disposable in order to survive.
    The attempt is held at the effective cap so a drift that repeats every tick
    creates no new labels; it is still released, never escalated."""
    intents, _ = decide(FleetState(), [claimed(attempts=2)], POLICY, [], T0)
    assert Release(task=1, reason="no_agent", attempt=2) in intents
    assert only(intents, Escalate) == []


def test_answered_task_drift_gets_the_raised_cap():
    """The drift release is held at the effective cap, not the policy's: an
    answered task on attempt 2 of 4 drifts to attempt 3. Catches the hold using
    `policy.max_attempts` directly."""
    intents, _ = decide(FleetState(), [claimed(attempts=2, answers=1)], POLICY, [], T0)
    assert intents == [Release(task=1, reason="no_agent", attempt=3)]


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
    task = claimed(packet={"to": "review", "attempt": 1, "head_sha": "a91c204"})
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
    """Cost is finalised by REAPING membership. Left out, a whole stage's spend
    -- every implementer that ever handed off -- is silently never counted, and
    the fleet ceiling is short by that much for the entire night."""
    from fleet.decide import charge_of
    from fleet.model import Retire

    agents = [AgentState(task=5, status="done", change_seq=9, cost_usd=1.25)]
    assert charge_of([Retire(task=5)], agents, {}) == {5: 1.25}


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
    task = claimed(packet={"to": "review", "attempt": 1, "head_sha": "a91c204"})
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
    assert set(vars(state)) == {"seen", "spent_usd", "dispatched", "charged"}


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


def test_a_closed_row_with_a_working_agent_is_retired():
    """Because _open ran before _reap, a closed row still carrying fleet:claimed
    produced no Release and therefore no StopAgent -- the agent ran on, its cost
    still counting against the fleet ceiling, with nothing able to stop it.

    Note the agent is WORKING, not done or stalled. Seeing the row is not
    enough: none of REAP's existing branches fire for a healthy agent, so
    widening the filter alone leaves the leak exactly where it was. A closed row
    needs a rule of its own.

    Retire rather than Release: the issue is closed, so relabelling it back to
    fleet:ready would leave a closed row advertising itself as available work."""
    closed = Task(
        number=5,
        title="t",
        state="claimed",
        area="eval",
        klass="patch",
        auto_ok=True,
        is_open=False,
    )
    agents = [AgentState(task=5, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [closed], POLICY, agents, T0)
    assert intents == [Retire(task=5)]


def test_a_closed_row_with_no_agent_produces_nothing():
    """The ordinary end state of every completed task. Emitting an intent for
    each of them would refill the run log with noise every tick, forever."""
    closed = Task(
        number=5, title="t", state="done", area="eval", klass="patch", auto_ok=True, is_open=False
    )
    intents, _ = decide(FleetState(), [closed], POLICY, [], T0)
    assert intents == []


def test_a_closed_claimed_row_is_reaped_before_its_area_is_reused():
    """m2-inputs §7's concrete case. The closed row is invisible to DISPATCH, so
    #8 goes out in the same tick -- which is correct ONLY because the reap
    precedes it and StopAgent lands before StartAgent."""
    closed = Task(
        number=5,
        title="t",
        state="claimed",
        area="eval",
        klass="patch",
        auto_ok=True,
        is_open=False,
    )
    open_row = Task(number=8, title="t", state="ready", area="eval", klass="patch", auto_ok=True)
    agents = [AgentState(task=5, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [closed, open_row], POLICY, agents, T0)
    stops = [n for n, i in enumerate(intents) if getattr(i, "task", None) == 5]
    starts = [n for n, i in enumerate(intents) if isinstance(i, Dispatch) and i.task == 8]
    assert stops and starts and stops[0] < starts[0]


def test_an_agent_on_a_non_live_row_is_orphaned_and_retired():
    """No phase reads a row in READY, DONE or BLOCKED as agent-bearing, so an
    agent sitting on one of those rows is invisible to every branch above --
    it holds a slot and an area forever, unstoppable, because nothing in this
    function's per-row loop ever looks at it. The rule this test pins lives
    after that loop: it walks the AGENTS, not the rows."""
    task = claimed(number=5, state="ready")
    agents = [AgentState(task=5, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert intents == [Retire(task=5)]


def test_an_agent_whose_task_has_no_ledger_row_at_all_is_orphaned_and_retired():
    """The same leak, one step further: not merely a row REAP's per-row loop
    skips, but no row at all -- a worktree for a task number the ledger has
    never heard of. Nothing but a walk over the agents themselves can catch
    this; there is no row to iterate."""
    agents = [AgentState(task=42, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [], POLICY, agents, T0)
    assert intents == [Retire(task=42)]


def test_an_orphan_already_reaped_this_pass_is_not_retired_twice():
    """A closed row in a non-live state (DONE, not CLAIMED) with a working agent
    is reaped by the closed-row rule above, in the per-row loop -- and that same
    task number is also absent from every live row, which is exactly the
    orphan condition. Without an explicit guard against a task the per-row loop
    already emitted for, this shape doubles the Retire and StopAgent lands
    twice for one agent."""
    closed = Task(
        number=5, title="t", state="done", area="eval", klass="patch", auto_ok=True, is_open=False
    )
    agents = [AgentState(task=5, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [closed], POLICY, agents, T0)
    assert intents == [Retire(task=5)]


def test_a_reviewer_that_writes_no_verdict_escalates_rather_than_looping():
    """Retiring it starts another reviewer, and another, until the fleet-wide
    budget drains -- one task consuming a whole night's ceiling. The state not
    having moved IS the evidence the stage produced nothing."""
    task = claimed(number=5, state="review")
    state = FleetState(dispatched=((5, "review"),))
    agents = [AgentState(task=5, status="done", change_seq=4)]
    intents, _ = decide(state, [task], POLICY, agents, T0)
    assert Escalate(task=5, reason="stage_produced_nothing") in intents
    assert Retire(task=5) not in intents


def test_the_previous_stage_s_agent_is_still_retired():
    """The normal handoff: the implementer wrote fleet:review itself and exited.
    We never continued this task INTO review, so its done agent is not this
    stage's."""
    task = claimed(number=5, state="review")
    agents = [AgentState(task=5, status="done", change_seq=4)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Retire(task=5) in intents


def test_a_continuation_that_moved_the_state_is_not_an_escalation():
    """The reviewer DID write its verdict: the task is at integrating while
    `dispatched` still says review. Comparing against the recorded state rather
    than merely 'was continued' is what keeps the happy path happy."""
    task = claimed(number=5, state="integrating")
    state = FleetState(dispatched=((5, "review"),))
    agents = [AgentState(task=5, status="done", change_seq=4)]
    intents, _ = decide(state, [task], POLICY, agents, T0)
    assert Retire(task=5) in intents


def test_the_record_is_dropped_when_the_agent_is_reaped():
    """Carried past the reap, the task's NEXT agent inherits a record from the
    dead one and its first clean finish reads as 'produced nothing' -- the same
    defect `seen` is pruned to avoid."""
    task = claimed(number=5, state="review")
    state = FleetState(dispatched=((5, "review"),))
    agents = [AgentState(task=5, status="done", change_seq=4)]
    _, after = decide(state, [task], POLICY, agents, T0)
    assert after.dispatched_in(5) is None


def test_a_continued_stage_is_recorded_against_the_state_it_was_started_in():
    """The other half of the record: without the write there is nothing for the
    check above to read, and every runaway stage reads as a handoff forever."""
    task = claimed(number=5, state="review")
    _, after = decide(FleetState(), [task], POLICY, [], T0)
    assert after.dispatched_in(5) == "review"


def test_a_working_agent_over_the_per_task_ceiling_on_a_continuation_escalates():
    """Task 3's gap: the CLAIMED branch checks cost, the continuation branch did
    not, so a reviewer that is healthy, moving and far over budget got no intent
    at all and ran until the whole fleet ceiling drained. Escalate rather than
    _fail: a Release relabels the task to READY and throws away a completed
    review, which is what this branch already refuses to do for a stall."""
    task = claimed(number=5, state="review")
    agents = [AgentState(task=5, status="working", change_seq=4, cost_usd=2.5)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert intents == [Escalate(task=5, reason="over_budget", detail="spent 2.50 of 2.00")]


def test_a_continuation_agent_exactly_at_the_ceiling_is_left_alone():
    """The boundary, pinned the way the CLAIMED branch's already is. Catches `>`
    flipped to `>=`, which kills a reviewer that has spent exactly its budget."""
    task = claimed(number=5, state="review")
    agents = [AgentState(task=5, status="working", change_seq=4, cost_usd=POLICY.per_task_usd)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert intents == []


#: A real integrator handback. `attempt` is required by handoff.schema.json
#: (spec §5.5) and `_is_rebase_handback` binds on it, so a fixture without
#: one is not a packet `fleet handoff` could ever have written -- and every
#: test below would then pass on the attempt mismatch rather than on the
#: rule it names. The tasks here all carry `attempts=1`.
HANDBACK = {"to": "claimed", "from": {"role": "integrator", "agent": "fleet-1"}, "attempt": 1}


def test_an_integrator_handback_releases_without_burning_an_attempt():
    """integrator.md step 3 hands a task back to CLAIMED on conflict, and
    nothing starts an agent at that moment -- so the next tick used to read it
    as drift and release it with attempt + 1."""
    task = claimed(number=4, state="claimed", attempts=1, packet=HANDBACK)
    intents, _ = decide(FleetState(), [task], POLICY, [], T0)
    assert intents == [Rebase(task=4, rebase=1)]


def test_a_second_handback_within_the_cap_counts_up():
    policy = dataclasses.replace(POLICY, max_rebases=2)
    task = claimed(number=4, state="claimed", attempts=1, rebases=1, packet=HANDBACK)
    intents, _ = decide(FleetState(), [task], policy, [], T0)
    assert intents == [Rebase(task=4, rebase=2)]


def test_a_handback_past_the_cap_escalates():
    """Parent §10's conflict storm: 'both -> NEEDS_HUMAN, stop integrating
    rather than thrash'. Without the cap a rebase that costs no attempt cycles
    forever, burning the fleet's budget on one task all night."""
    task = claimed(number=4, state="claimed", attempts=1, rebases=1, packet=HANDBACK)
    intents, _ = decide(FleetState(), [task], POLICY, [], T0)
    assert intents == [Escalate(task=4, reason="rebases_exhausted")]


def test_answers_raise_rebase_cap():
    """Two answers on a one-rebase cap allow rebase 3. Catches the rebase cap
    ignoring answers, and answers treated as a flag rather than a count."""
    task = claimed(number=4, state="claimed", attempts=1, rebases=2, answers=2, packet=HANDBACK)
    intents, _ = decide(FleetState(), [task], POLICY, [], T0)
    assert intents == [Rebase(task=4, rebase=3)]


def test_answered_rebase_loop_escalates():
    """The raised rebase cap still ends. With max_rebases=1 and one answer the
    cap is 2: rebase 2 is allowed, a third handback escalates."""
    at_one = claimed(number=4, state="claimed", attempts=1, rebases=1, answers=1, packet=HANDBACK)
    intents, _ = decide(FleetState(), [at_one], POLICY, [], T0)
    assert intents == [Rebase(task=4, rebase=2)]
    at_two = claimed(number=4, state="claimed", attempts=1, rebases=2, answers=1, packet=HANDBACK)
    intents, _ = decide(FleetState(), [at_two], POLICY, [], T0)
    assert intents == [Escalate(task=4, reason="rebases_exhausted")]


def test_a_claimed_task_with_no_agent_and_no_handback_is_still_drift():
    """The drift rule must survive: a daemon restart, a crashed pane, a killed
    agent. Only an integrator's handback packet is exempt."""
    task = claimed(number=4, state="claimed", attempts=1)
    intents, _ = decide(FleetState(), [task], POLICY, [], T0)
    assert intents == [Release(task=4, reason="no_agent", attempt=2)]


def test_a_handback_from_a_non_integrator_is_not_a_rebase():
    """Only the integrator hands back for a rebase. A packet claiming otherwise
    would let any role dodge an attempt by naming the right target state."""
    packet = {"to": "claimed", "from": {"role": "implementer", "agent": "fleet-2"}, "attempt": 1}
    task = claimed(number=4, state="claimed", attempts=1, packet=packet)
    intents, _ = decide(FleetState(), [task], POLICY, [], T0)
    assert intents == [Release(task=4, reason="no_agent", attempt=2)]


def test_a_handback_still_showing_a_done_agent_is_a_rebase_not_a_phantom_advance():
    """Ruling B3. An integrator that just relabeled to CLAIMED and exited still
    reports status="done" for one tick before StopAgent removes it from herdr's
    snapshot -- `_post` writes the comment, then moves the label, so this row
    reads as CLAIMED with a done agent and a handback packet in the same
    fetch. Without the handback check running ahead of `_advance`, this done
    agent reads as a clean exit and `_advance` emits a meaningless
    Advance(task=4, to='claimed') self-transition instead of spending the
    rebase."""
    task = claimed(number=4, state="claimed", attempts=1, packet=HANDBACK)
    agents = [AgentState(task=4, status="done", change_seq=7)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert intents == [Rebase(task=4, rebase=1)]


def test_a_handback_visible_before_the_label_catches_up_still_wins_over_retire():
    """The other half of ruling B3's precedence. `_post` posts the handback
    comment BEFORE it moves the label (commands.py `_post`), so a tick that
    reads the issue in that exact window sees the packet with `to: claimed`
    while the state label still says INTEGRATING. That row falls into the
    non-claimed body, whose done-agent branch is Retire -- and without the
    handback check running ahead of it, this handback would be read as an
    ordinary stage handoff and its agent merely retired, one tick early and
    with no rebase spent."""
    task = claimed(number=4, state="integrating", attempts=1, packet=HANDBACK)
    agents = [AgentState(task=4, status="done", change_seq=7)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert intents == [Rebase(task=4, rebase=1)]


def test_a_working_agent_with_a_stale_handback_packet_is_left_alone():
    """`packet.parse_latest` returns the newest handoff comment across the
    whole issue's history with no per-attempt scoping (spec §5.5) -- Task 7,
    not this one, binds a packet to its attempt. After a legitimate rebase and
    re-dispatch, the integrator's original handback stays "latest" until the
    new implementer posts its own, which can be many ticks: a task at
    rebases=1, re-dispatched to CLAIMED, its new implementer healthy and
    working, still carries the stale handback packet from the rebase that
    already happened. Reading it unconditionally would escalate a healthy task
    to NEEDS_HUMAN one tick after the rebase that just succeeded -- the exact
    loop this branch exists to bound, inverted."""
    task = claimed(number=4, state="claimed", attempts=1, rebases=1, packet=HANDBACK)
    agents = [AgentState(task=4, status="working", change_seq=3, cost_usd=0.1)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert intents == []


def test_a_dirty_agent_with_a_stale_handback_packet_still_fails_normally():
    """The sibling case: a dirty exit must take its existing `_fail` branch
    rather than being read as a rebase handback because a stale packet happens
    to be sitting on the issue history."""
    task = claimed(number=4, state="claimed", attempts=1, rebases=1, packet=HANDBACK)
    agents = [AgentState(task=4, status="dirty", change_seq=3)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert intents == [Release(task=4, reason="dirty_exit", attempt=2)]


def test_a_finished_continuation_agent_over_budget_is_not_charged_for_it():
    """Status first, cost second, here as in the CLAIMED branch. The stage
    finished; its work is already paid for, and escalating instead of retiring
    would strand a completed review at NEEDS_HUMAN over arithmetic."""
    task = claimed(number=5, state="review")
    agents = [AgentState(task=5, status="done", change_seq=4, cost_usd=2.5)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Retire(task=5) in intents
    assert only(intents, Escalate) == []


def test_a_packet_from_a_previous_attempt_does_not_advance_the_task():
    """M3's shell fetches the newest handoff comment off an issue that outlives
    the attempt, so attempt 1's packet is readable on attempt 2. Acting on it
    advances work that never happened: a task released for a dirty exit,
    re-dispatched, and then moved to REVIEW on the failed attempt's evidence."""
    stale = {"to": "review", "attempt": 1, "from": {"role": "implementer"}}
    task = claimed(number=5, state="claimed", attempts=2, packet=stale)
    agents = [AgentState(task=5, status="done", change_seq=4)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert not any(isinstance(i, Advance) for i in intents)


def test_a_packet_from_the_current_attempt_advances():
    fresh = {"to": "review", "attempt": 2, "from": {"role": "implementer"}}
    task = claimed(number=5, state="claimed", attempts=2, packet=fresh)
    agents = [AgentState(task=5, status="done", change_seq=4)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Advance(task=5, to="review") in intents


def test_a_packet_with_no_attempt_field_does_not_advance():
    """A packet written before the schema required `attempt` -- or by a CLI that
    was not upgraded. Treating a missing field as a match is the permissive
    default that makes the whole check decorative."""
    task = claimed(number=5, state="claimed", attempts=1, packet={"to": "review"})
    agents = [AgentState(task=5, status="done", change_seq=4)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert not any(isinstance(i, Advance) for i in intents)


# ---------------------------------------------------- an OPEN row at DONE


def test_an_open_row_at_done_is_closed():
    """Spec §6: reaching DONE closes the issue. Nothing in the real shell did.

    `expand(Advance(to="done"))` appends CloseIssue, but an integrator reaches
    DONE by running `fleet handoff --to done`, and `commands._post` moves the
    label itself. The next tick therefore reads `fleet:done`, which is not in
    `_LIVE`, so the row loop skipped it before `_advance` was reachable at all
    -- and the orphan pass emitted `Retire`, whose expansion deliberately
    carries no relabel and no close. The row stayed OPEN forever, spec §6 was
    unmet, and `_unblock`'s closed-dependency rule had nothing to read.
    """
    done = Task(
        number=5, title="t", state="done", area="eval", klass="patch", auto_ok=True, is_open=True
    )
    agents = [AgentState(task=5, status="done", change_seq=3)]
    intents, _ = decide(FleetState(), [done], POLICY, agents, T0)
    assert intents == [Close(task=5)]


def test_an_open_row_at_done_with_no_agent_is_left_alone():
    """The gate, and it is the rule rather than an optimisation.

    An OPEN row at `fleet:done` with no agent is not the fleet's work
    finishing -- it is a row somebody else labelled. blocked-self-resolves.yaml
    is the standing case: issue 11 is a dependency recorded as complete by hand,
    OPEN and `fleet:done`, and the allocator has never run it. Closing on the
    strength of a label a human wrote would have the allocator closing issues it
    never touched, and `_unblock` already reads `state == "done"` as satisfied
    without needing the row closed.
    """
    done = Task(
        number=5, title="t", state="done", area="eval", klass="patch", auto_ok=True, is_open=True
    )
    assert decide(FleetState(), [done], POLICY, [], T0)[0] == []


def test_a_close_that_does_not_land_is_retried_on_the_next_tick():
    """Idempotence, by order rather than by memory.

    `expand(Close)` closes before it stops, so a gh outage that fails the close
    leaves both of this branch's conditions true -- the row still OPEN, the
    agent still live -- and the next tick arrives here and tries again. Stopping
    first would spend the one piece of evidence that re-arms the retry, and the
    row would sit OPEN at `fleet:done` for the rest of the night.
    """
    done = Task(
        number=5, title="t", state="done", area="eval", klass="patch", auto_ok=True, is_open=True
    )
    agents = [AgentState(task=5, status="done", change_seq=3)]
    state = FleetState()
    for minute in range(3):
        intents, state = decide(state, [done], POLICY, agents, T0 + timedelta(minutes=minute))
        assert intents == [Close(task=5)]


def test_a_row_at_done_draws_exactly_one_intent():
    """The orphan pass's `handled` guard, restated for this row. A task at DONE
    with an agent is also, verbatim, the orphan condition -- an agent on a row
    that is not live -- so without the guard it draws Close AND Retire, two
    intents for one task in one tick, two StopAgents, and two run-log lines for
    one event."""
    done = Task(
        number=5, title="t", state="done", area="eval", klass="patch", auto_ok=True, is_open=True
    )
    agents = [AgentState(task=5, status="working", change_seq=3)]
    intents, _ = decide(FleetState(), [done], POLICY, agents, T0)
    assert len(intents) == 1
    assert only(intents, Retire) == []


def test_a_closed_row_at_done_produces_nothing_however_many_ticks_pass():
    """The bound on the rule above. Once the close lands the row is CLOSED with
    no agent, and it must produce nothing at all, forever -- an intent per
    closed row per tick floods the run log with a fleet's worth of no-ops, and
    every one of them is a gh call."""
    closed = Task(
        number=5, title="t", state="done", area="eval", klass="patch", auto_ok=True, is_open=False
    )
    state = FleetState()
    for minute in range(5):
        intents, state = decide(state, [closed], POLICY, [], T0 + timedelta(minutes=minute))
        assert intents == []


def test_closing_a_done_row_charges_its_agent_once():
    """`Close` stops the agent, so that agent's cost is final on the tick it
    fires -- it belongs in REAPING for the same reason Retire does. Left out,
    the integrator's whole spend is never counted against the ceiling."""
    done = Task(
        number=5, title="t", state="done", area="eval", klass="patch", auto_ok=True, is_open=True
    )
    agents = [AgentState(task=5, status="done", change_seq=3, cost_usd=1.75)]
    _, state = decide(FleetState(), [done], POLICY, agents, T0)
    assert state.spent_usd == 1.75


# ------------------------------------------------- cost is charged once, ever


def _ready(number=5, **kw):
    return Task(
        number=number, title="t", state="ready", area="eval", klass="patch", auto_ok=True, **kw
    )


def test_an_agent_reaped_on_every_tick_is_charged_once():
    """The stubborn-stop loop, which is not hypothetical.

    When `stop_agent` fails for any reason outside `AGENT_ABSENT_CODES` the
    executor deliberately leaves the agent alive -- it may still be running --
    and the orphan pass re-emits `Retire` for it on the next tick, and the next.
    `_final_cost` summed `AgentState.cost_usd`, which is CUMULATIVE, with no
    memory across ticks: a $2 agent added $2 to `spent_usd` every tick and
    drained a $25 fleet in thirteen. The run log carried the same inflation into
    any restart, so the fleet came back already drained on money it never spent.
    """
    agents = [AgentState(task=5, status="working", change_seq=3, cost_usd=2.0)]
    state = FleetState()
    for minute in range(6):
        intents, state = decide(state, [_ready()], POLICY, agents, T0 + timedelta(minutes=minute))
        assert only(intents, Retire) == [Retire(task=5)]
    assert state.spent_usd == 2.0


def test_a_reaped_agent_that_keeps_spending_is_charged_the_increment():
    """Once is not the same as never again. The stop failed and the agent is
    still running, so it is still spending -- and those new dollars are real.
    Charging the delta is what makes "counted once" a statement about each
    dollar rather than about the agent."""
    state = FleetState()
    intents, state = decide(
        state,
        [_ready()],
        POLICY,
        [AgentState(task=5, status="working", change_seq=3, cost_usd=2.0)],
        T0,
    )
    assert state.spent_usd == 2.0
    _, state = decide(
        state,
        [_ready()],
        POLICY,
        [AgentState(task=5, status="working", change_seq=4, cost_usd=3.25)],
        T0 + timedelta(minutes=1),
    )
    assert state.spent_usd == 3.25


def test_a_replacement_agent_on_the_same_task_is_charged_in_full():
    """The memory is per AGENT, keyed by task, and a Dispatch or Continue starts
    a new one -- new worktree, new session, `cost_usd` back at zero. Keeping the
    old agent's total against the task would net the replacement's whole spend
    off to nothing, which is the mirror of the defect above and just as silent.
    """
    policy = dataclasses.replace(POLICY, max_attempts=5)
    state = FleetState()
    _, state = decide(
        state,
        [claimed(number=5)],
        policy,
        [AgentState(task=5, status="dirty", change_seq=1, cost_usd=2.0)],
        T0,
    )
    assert state.spent_usd == 2.0
    # The Release moved #5 back to READY; the next tick dispatches it again.
    intents, state = decide(state, [_ready()], policy, [], T0 + timedelta(minutes=1))
    assert only(intents, Dispatch)
    # A second attempt, a second agent, its own dollars.
    _, state = decide(
        state,
        [claimed(number=5, attempts=2)],
        policy,
        [AgentState(task=5, status="dirty", change_seq=1, cost_usd=1.5)],
        T0 + timedelta(minutes=2),
    )
    assert state.spent_usd == 3.5


# --------------------------------------- a handback is bound to its attempt too


def test_a_handback_from_a_previous_attempt_is_not_a_rebase():
    """Spec §5.5, applied to the branch that ran ahead of every other one.

    `packet.parse_latest` returns the newest handoff comment across the whole
    issue's history with no per-attempt scoping, and the issue outlives the
    attempt. So attempt 1's integrator handback is still "latest" while the task
    is on attempt 2 -- and `_is_rebase_handback` read it with no attempt binding
    at all, ahead of every other REAP branch. The task was charged a rebase it
    did not make, hit `max_rebases`, and went to NEEDS_HUMAN naming
    `rebases_exhausted` for work that never happened. `_advance` implements
    exactly this check on exactly this shape; this path did not.

    The drift release's attempt is held at the effective cap (2 here) so a
    drift that repeats every tick creates no new labels; it is still a
    release, never an escalation.
    """
    stale = claimed(
        number=4,
        attempts=2,
        rebases=1,
        packet={"to": "claimed", "from": {"role": "integrator"}, "attempt": 1},
    )
    intents, _ = decide(FleetState(), [stale], POLICY, [], T0)
    assert only(intents, Escalate) == []
    assert intents == [Release(task=4, reason="no_agent", attempt=2)]


def test_a_handback_with_no_attempt_at_all_is_not_a_rebase():
    """A missing `attempt` is a mismatch, not a pass -- the same rule `_advance`
    states. `handoff.schema.json` requires the field, so a packet without one
    predates spec §5.5 or was not written by `fleet handoff`; treating it as
    matching would make the whole check decorative."""
    stale = claimed(
        number=4, attempts=1, rebases=0, packet={"to": "claimed", "from": {"role": "integrator"}}
    )
    intents, _ = decide(FleetState(), [stale], POLICY, [], T0)
    assert only(intents, Rebase) == []
    assert intents == [Release(task=4, reason="no_agent", attempt=2)]


def test_a_handback_from_this_attempt_still_rebases():
    """The other half, or the fix is just a disabled feature. A packet whose
    attempt matches the task's is the live conflict `max_rebases` bounds."""
    current = claimed(
        number=4,
        attempts=2,
        rebases=0,
        packet={"to": "claimed", "from": {"role": "integrator"}, "attempt": 2},
    )
    intents, _ = decide(FleetState(), [current], POLICY, [], T0)
    assert intents == [Rebase(task=4, rebase=1)]


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
DECLARED = (("implementer", "claude", "claude-opus-5"),)


def _mismatch_case(observed_model: str | None):
    """One CLAIMED patch task with a live agent reporting `observed_model`."""
    task = claimed(1)
    agent = AgentState(task=1, status="working", change_seq=5, cost_usd=0.1, model=observed_model)
    policy = dataclasses.replace(POLICY, agents=DECLARED)
    intents, _ = decide(prev=FleetState(), ledger=[task], policy=policy, agents=[agent], now=NOW)
    return intents


def test_an_agent_running_a_model_nobody_declared_is_escalated():
    """UnpricedModel cannot see this: a silent substitution between two PRICED
    models fires no exception and bills the night at a rate nobody chose.
    Catches trusting the transcript as the only source of truth."""
    assert Escalate(task=1, reason="model_mismatch") in _mismatch_case("claude-opus-4-8")


def test_an_agent_running_the_declared_model_is_left_alone():
    """Catches a comparison that fires on every agent -- which would escalate
    the whole backlog on the first tick."""
    assert not [i for i in _mismatch_case("claude-opus-5") if isinstance(i, Escalate)]


def test_an_agent_whose_model_is_not_yet_known_is_left_alone():
    """A transcript not yet written reports model=None. Catches escalating
    every agent in the seconds after dispatch, when nothing is yet known."""
    assert not [i for i in _mismatch_case(None) if isinstance(i, Escalate)]


def test_an_agent_observed_running_two_models_is_escalated():
    """parse_snapshot records the sentinel MULTIPLE_MODELS when a transcript
    names more than one model carrying tokens -- a fallback substitution mid
    session. Catches collapsing that to None, which would make the very case
    §4 exists to catch pass vacuously."""
    from fleet.model import MULTIPLE_MODELS

    assert Escalate(task=1, reason="model_mismatch") in _mismatch_case(MULTIPLE_MODELS)


def test_a_reviewer_running_a_model_nobody_declared_is_escalated():
    """The CLAIMED path is not the only live state: a reviewer runs long
    enough to burn real money at a rate nobody chose, and it is continued
    rather than dispatched, so it never passes through the CLAIMED branch at
    all. Catches the REVIEW/INTEGRATING branch having no model-mismatch check
    of its own -- the gap the CLAIMED-only version of this feature left open."""
    task = claimed(number=5, state="review")
    agents = [
        AgentState(task=5, status="working", change_seq=4, cost_usd=0.1, model="claude-opus-4-8")
    ]
    policy = dataclasses.replace(POLICY, agents=(("reviewer", "claude", "claude-opus-5"),))
    intents, _ = decide(FleetState(), [task], policy, agents, T0)
    assert Escalate(task=5, reason="model_mismatch") in intents


def test_a_reviewer_running_the_declared_model_is_left_alone():
    """Catches a comparison that fires on every reviewer regardless of match --
    which would escalate every healthy review in progress, the same failure
    the CLAIMED-path sibling test guards against."""
    task = claimed(number=5, state="review")
    agents = [
        AgentState(task=5, status="working", change_seq=4, cost_usd=0.1, model="claude-opus-5")
    ]
    policy = dataclasses.replace(POLICY, agents=(("reviewer", "claude", "claude-opus-5"),))
    intents, _ = decide(FleetState(), [task], policy, agents, T0)
    assert intents == []


def test_a_claimed_task_with_no_route_is_now_visible_to_unroutable():
    """`_STAGE` gained a `"claimed": 0` row so `_model_mismatch` could resolve
    a CLAIMED task's role, but `_STAGE` also gates `_unroutable`
    (`if task.state not in _STAGE or not task.auto_ok: continue`), so the same
    row widens what that phase inspects: a CLAIMED, auto-ok task whose class
    has no route is now a policy error `_unroutable` can name, where it was
    previously invisible to it. Catches removing `"claimed"` from `_STAGE` --
    which would silence this AND break every `_model_mismatch` test above."""
    task = Task(
        number=9, title="t", state="claimed", area="eval", klass=None, auto_ok=True, attempts=1
    )
    intents, _ = decide(FleetState(), [task], POLICY, [], T0)
    assert Escalate(task=9, reason="unroutable") in intents


def test_a_working_agent_whose_transcript_grows_is_not_stalled():
    """Issue #10. herdr's `state_change_seq` moves only on a status change, so
    an agent working through one long turn holds the same sequence for the
    whole turn and was released as `stalled` while it worked. Transcript growth
    is the signal that moves with real work. Catches an `_observe` that keys on
    the sequence alone: the timer started at T0 is never reset and the task is
    released at the tenth minute."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0, transcript_bytes=100)),))
    agents = [AgentState(task=1, status="working", change_seq=10, transcript_bytes=200)]
    _, state = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=9))
    intents, _ = decide(state, [claimed()], POLICY, agents, T0 + timedelta(minutes=10))
    assert intents == []


def test_a_working_agent_whose_sequence_and_transcript_both_hold_is_stalled():
    """The other side: neither signal moving is a stall. Catches an `_observe`
    that resets the timer every tick regardless, under which no agent is ever
    released."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0, transcript_bytes=100)),))
    agents = [AgentState(task=1, status="working", change_seq=10, transcript_bytes=100)]
    intents, _ = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=10))
    assert Release(task=1, reason="stalled", attempt=2) in intents


def _chain_policy():
    return dataclasses.replace(
        POLICY,
        routes=(*POLICY.routes, ("build", ("chain",))),
        agents=(("chain", "chain", "chain"),),
        ceilings=(("chain", 40.0),),
    )


def test_chain_agent_under_own_ceiling_not_over_budget():
    """A chain run spends 30 against a global ceiling of 2 and its own of 40.
    Catches the over-budget check still reading `policy.per_task_usd`."""
    task = dataclasses.replace(claimed(), klass="build")
    agents = [AgentState(task=1, status="working", change_seq=10, cost_usd=30.0)]
    intents, _ = decide(FleetState(), [task], _chain_policy(), agents, T0)
    assert only(intents, Release) == []
    assert only(intents, Escalate) == []


def test_chain_agent_over_own_ceiling():
    task = dataclasses.replace(claimed(), klass="build")
    agents = [AgentState(task=1, status="working", change_seq=10, cost_usd=41.0)]
    intents, _ = decide(FleetState(), [task], _chain_policy(), agents, T0)
    assert Release(task=1, reason="over_budget", attempt=2) in intents


def test_later_stage_over_budget_uses_the_roles_ceiling():
    """The REVIEW-stage branch, which escalates rather than releases. A reviewer
    on a kind with a 5.0 ceiling at cost 3 is under it; at 6 it escalates and
    the detail names the ceiling it was measured against."""
    policy = dataclasses.replace(
        POLICY,
        agents=(("reviewer", "codex", "m"),),
        ceilings=(("codex", 5.0),),
    )
    task = claimed(state="review")
    under = [AgentState(task=1, status="working", change_seq=10, cost_usd=3.0)]
    intents, _ = decide(FleetState(), [task], policy, under, T0)
    assert only(intents, Escalate) == []
    over = [AgentState(task=1, status="working", change_seq=10, cost_usd=6.0)]
    intents, _ = decide(FleetState(), [task], policy, over, T0)
    assert Escalate(task=1, reason="over_budget", detail="spent 6.00 of 5.00") in intents
