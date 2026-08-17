from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from fleet.decide import decide
from fleet.model import AgentState, Dispatch, Drain, FleetState, Release, Task
from test_decide_reap import POLICY

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)


def ready(number, area="eval", klass="patch", auto_ok=True, attempts=0):
    return Task(
        number=number,
        title="t",
        state="ready",
        area=area,
        klass=klass,
        auto_ok=auto_ok,
        attempts=attempts,
    )


def live(number=1, area="eval", klass="patch"):
    return Task(
        number=number,
        title="t",
        state="claimed",
        area=area,
        klass=klass,
        auto_ok=True,
    )


def dispatched(intents):
    return sorted(i.task for i in intents if isinstance(i, Dispatch))


def test_a_ready_auto_ok_task_is_dispatched_to_the_first_role_in_its_route():
    intents, _ = decide(FleetState(), [ready(1)], POLICY, [], T0)
    assert Dispatch(task=1, role="implementer", base="integration", attempt=1) in intents


def test_dispatch_bases_the_agent_on_the_policys_integration_branch():
    """Catches a hardcoded "integration". A repo whose integration branch is
    named anything else would have every agent branched off the wrong base, and
    the fixture's own fleet.toml is the only reason the literal looks right."""
    policy = dataclasses.replace(POLICY, integration_branch="trunk")
    intents, _ = decide(FleetState(), [ready(1)], policy, [], T0)
    assert Dispatch(task=1, role="implementer", base="trunk", attempt=1) in intents


def test_a_re_dispatched_task_keeps_the_attempt_number_it_already_carries():
    """One writer for the counter: decide() computes the number and expand()
    writes it. Catches `attempt=task.attempts + 1`, which counts a dispatch as a
    failure and reaches the cap after one real try."""
    intents, _ = decide(FleetState(), [ready(1, attempts=2)], POLICY, [], T0)
    assert Dispatch(task=1, role="implementer", base="integration", attempt=2) in intents


def test_two_ready_tasks_in_one_area_yield_exactly_one_dispatch():
    """Parent §11's first case, and harness §4's issues 2 and 3."""
    intents, _ = decide(FleetState(), [ready(2, "train"), ready(3, "train")], POLICY, [], T0)
    assert len(dispatched(intents)) == 1


def test_two_ready_tasks_in_different_areas_both_dispatch():
    """Catches an exclusion that locks the whole fleet rather than one area."""
    intents, _ = decide(FleetState(), [ready(1, "eval"), ready(2, "train")], POLICY, [], T0)
    assert dispatched(intents) == [1, 2]


def test_an_area_already_held_by_a_claimed_task_is_excluded():
    agents = [AgentState(task=1, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [live(), ready(8, "eval")], POLICY, agents, T0)
    assert dispatched(intents) == []


def test_an_area_held_by_a_task_in_review_is_excluded():
    """REVIEW is live: the task keeps its area until `fleet verdict` moves it
    out. Catches a live set of CLAIMED alone, which puts a second agent into an
    area a reviewer is still reading."""
    reviewing = Task(
        number=1,
        title="t",
        state="review",
        area="eval",
        klass="patch",
        auto_ok=True,
    )
    intents, _ = decide(FleetState(), [reviewing, ready(8, "eval")], POLICY, [], T0)
    assert dispatched(intents) == []


def test_a_closed_task_in_a_live_state_holds_neither_a_slot_nor_an_area():
    """Captured row #12 is exactly this: CLOSED, still labelled fleet:review and
    area:docs. Keying on the state label alone made it consume a slot and lock
    area:docs permanently, with nothing able to release it -- REAP only touches
    CLAIMED, and nobody runs `fleet verdict` on a closed issue."""
    small = dataclasses.replace(POLICY, slots=1)
    closed = Task(
        number=12,
        title="t",
        state="review",
        area="eval",
        klass="patch",
        auto_ok=True,
        is_open=False,
    )
    intents, _ = decide(FleetState(), [closed, ready(8, "eval")], small, [], T0)
    assert dispatched(intents) == [8]


def test_a_closed_task_is_never_dispatched():
    """The other direction: a READY row that has been closed out from under the
    fleet is not work to hand an agent."""
    closed = dataclasses.replace(ready(1), is_open=False)
    intents, _ = decide(FleetState(), [closed], POLICY, [], T0)
    assert dispatched(intents) == []


def test_a_task_without_auto_ok_is_never_dispatched():
    intents, _ = decide(FleetState(), [ready(6, "configs", auto_ok=False)], POLICY, [], T0)
    assert dispatched(intents) == []


def test_auto_ok_is_read_from_the_label_and_never_inferred_from_area():
    """m2-inputs §2: ownership.md maps both configs/** and gates/** to
    area:configs, and gates/** is separately non-dispatchable. Issue 6 is
    legitimately area:configs AND auto-ok, because its criteria forbid touching
    gates/. An allocator gating on area would refuse it -- one of the four hard
    fails -- or, inverted, permit a gates/ edit."""
    intents, _ = decide(FleetState(), [ready(6, "configs", auto_ok=True)], POLICY, [], T0)
    assert dispatched(intents) == [6]


def test_dispatch_stops_entirely_while_an_integration_task_is_live():
    """Harness amendment 2: the integrator touches every area by definition, so
    per-area exclusion does not cover it. A global exclusive lock does."""
    integrating = live(number=20, area=None, klass="integration")
    agents = [AgentState(task=20, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [integrating, ready(1, "eval")], POLICY, agents, T0)
    assert dispatched(intents) == []


def test_slots_bound_the_number_of_dispatches():
    small = dataclasses.replace(POLICY, slots=2)
    backlog = [ready(1, "eval"), ready(2, "train"), ready(3, "models"), ready(4, "data")]
    intents, _ = decide(FleetState(), backlog, small, [], T0)
    assert len(dispatched(intents)) == 2


def test_a_live_task_consumes_a_slot_even_though_it_is_not_ready():
    """Slots are counted off the ledger's live tasks, not off the dispatches
    this tick. Catches `free = policy.slots`, which would run one agent per slot
    on top of everything already running."""
    small = dataclasses.replace(POLICY, slots=1)
    agents = [AgentState(task=1, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [live(), ready(2, "train")], small, agents, T0)
    assert dispatched(intents) == []


def test_at_eighty_percent_of_budget_the_fleet_drains_rather_than_killing():
    """Parent §10: drain, do not kill. In-flight tasks finish; nothing new
    starts. Killing at the ceiling throws away work already paid for."""
    spent = FleetState(spent_usd=20.0)  # 80% of 25.0
    intents, _ = decide(spent, [ready(1, "eval")], POLICY, [], T0)
    assert dispatched(intents) == []
    assert Drain(reason="budget") in intents


def test_money_committed_to_live_agents_counts_toward_the_drain():
    """The running total holds only the cost of agents already reaped, so the
    threshold has to add what the live ones have spent so far. Catches a drain
    test that reads spent_usd alone and keeps dispatching while six agents burn
    the rest of the fleet budget."""
    agents = [AgentState(task=1, status="working", change_seq=1, cost_usd=1.5)]
    intents, _ = decide(FleetState(spent_usd=19.0), [live(), ready(2, "train")], POLICY, agents, T0)
    assert dispatched(intents) == []
    assert Drain(reason="budget") in intents


def test_below_the_drain_threshold_dispatch_continues():
    """Catches a threshold that fires early -- e.g. one that re-adds a
    cumulative per-agent total every tick."""
    intents, _ = decide(FleetState(spent_usd=19.9), [ready(1, "eval")], POLICY, [], T0)
    assert dispatched(intents) == [1]


def test_draining_does_not_stop_reaping_or_unblocking():
    """Drain stops dispatch only. A stalled agent must still be released, or the
    fleet drains into a state nothing cleans up."""
    intents, _ = decide(FleetState(spent_usd=25.0), [live()], POLICY, [], T0)
    assert Release(task=1, reason="no_agent", attempt=1) in intents


def test_a_task_in_triage_is_never_dispatched():
    """Discoveries land in TRIAGE. An agent that could both file and claim work
    has authorized itself."""
    triage = Task(number=13, title="t", state="triage", area="data", klass=None, auto_ok=False)
    intents, _ = decide(FleetState(), [triage], POLICY, [], T0)
    assert dispatched(intents) == []


def test_a_class_with_no_route_is_not_dispatched():
    """Catches `route[0]` on an empty route, an IndexError out of the pure
    core."""
    intents, _ = decide(FleetState(), [ready(9, "docs", klass="spec")], POLICY, [], T0)
    assert dispatched(intents) == []
