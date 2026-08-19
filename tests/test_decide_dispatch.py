from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from fleet.decide import decide
from fleet.model import (
    AgentState,
    Continue,
    Dispatch,
    Drain,
    Escalate,
    FleetState,
    Release,
    Task,
)
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


def staged(number, state, area="eval", klass="patch"):
    return Task(number=number, title="t", state=state, area=area, klass=klass, auto_ok=True)


def dispatched(intents):
    return sorted(i.task for i in intents if isinstance(i, Dispatch))


def continued(intents):
    return sorted(i.task for i in intents if isinstance(i, Continue))


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
    # #1 is CONTINUED to its reviewer -- not new work, and not a Dispatch. #8 is
    # the assertion that matters: the area is still excluded.
    assert dispatched(intents) == []
    assert continued(intents) == [1]


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


def test_a_task_in_integrating_arms_the_global_lock():
    """INTEGRATING is the state a real integrator lands in -- `fleet verdict
    --verdict approved` puts it there -- and the lock was proven only via a
    CLAIMED integration task, so dropping "integrating" from `_LIVE` entirely
    passed every test while unlocking the fleet under a running integrator."""
    integrating = Task(
        number=20,
        title="t",
        state="integrating",
        area=None,
        klass="integration",
        auto_ok=True,
    )
    intents, _ = decide(FleetState(), [integrating, ready(1, "eval")], POLICY, [], T0)
    assert dispatched(intents) == []


def test_a_task_in_integrating_holds_its_slot_and_its_area():
    """The other half of the same membership: INTEGRATING is live, so it counts
    against slots and excludes its area."""
    small = dataclasses.replace(POLICY, slots=1)
    integrating = Task(
        number=20,
        title="t",
        state="integrating",
        area="eval",
        klass="patch",
        auto_ok=True,
    )
    intents, _ = decide(FleetState(), [integrating, ready(8, "eval")], small, [], T0)
    assert dispatched(intents) == []


# The shared POLICY routes `patch` alone, which is enough to prove the lock
# against a task that is already live -- a live task needs no route. Dispatching
# an integration task does need one.
ROUTES_INTEGRATION = dataclasses.replace(
    POLICY, routes=(*POLICY.routes, ("integration", ("integrator",)))
)


def test_only_one_integration_task_starts_in_a_tick():
    """The other half of amendment 2's lock, and the half the live-task test
    above cannot reach. `any(t.klass == "integration" for t in live)` is computed
    once, before the loop, off tasks that were ALREADY live -- so two READY
    integration tasks and nothing live arms nothing, and both are dispatched into
    the same tick. Two integrators rebasing onto the integration branch at once
    is precisely the conflict the lock exists to prevent, and per-area exclusion
    cannot catch it because an integration task carries no area."""
    intents, _ = decide(
        FleetState(),
        [ready(20, area=None, klass="integration"), ready(21, area=None, klass="integration")],
        ROUTES_INTEGRATION,
        [],
        T0,
    )
    assert dispatched(intents) == [20]


def test_a_second_integration_task_does_not_block_the_rest_of_the_tick():
    """The lock is one integrator at a time, not one dispatch at a time. Catches
    a fix that `break`s out of the loop on the second integration task and so
    silently drops every ordinary task numbered above it."""
    backlog = [
        ready(20, area=None, klass="integration"),
        ready(21, area=None, klass="integration"),
        ready(22, "eval"),
    ]
    intents, _ = decide(FleetState(), backlog, ROUTES_INTEGRATION, [], T0)
    assert dispatched(intents) == [20, 22]


def test_a_task_that_already_has_a_live_agent_is_not_dispatched_again():
    """Slots and exclusion are counted off the ledger's labels, while REAP and
    the budget drain read the agent list. When the two disagree -- a StopAgent
    that failed while its SetLabels landed, so the row reads READY under a
    running agent -- nothing notices, and the allocator starts a SECOND agent on
    a task that already has one, in the same worktree."""
    agents = [AgentState(task=1, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [ready(1, "eval")], POLICY, agents, T0)
    assert dispatched(intents) == []


def test_a_running_agent_consumes_a_slot_whatever_its_row_says():
    """`slots` bounds concurrent agents, so an agent the ledger has lost track of
    still costs one. Catches counting free slots off live LABELS alone, which
    lets drift push the fleet past its own ceiling."""
    small = dataclasses.replace(POLICY, slots=1)
    agents = [AgentState(task=1, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [ready(1, "eval"), ready(2, "train")], small, agents, T0)
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


def test_a_ready_task_the_policy_cannot_route_is_escalated():
    """Not dispatching it is right; saying nothing about it is not. A READY,
    auto-ok task whose class has no route was skipped silently on every tick --
    no intent, no runlog line, and `fleet plan` printing "no action" -- so it sat
    in READY for the whole night with nothing able to notice. A human is the only
    thing that can route it, so name one."""
    intents, _ = decide(FleetState(), [ready(9, "docs", klass="spec")], POLICY, [], T0)
    assert Escalate(task=9, reason="unroutable") in intents


def test_a_ready_task_with_no_class_label_at_all_is_escalated():
    """The other way to be unroutable: no `class:` label, so `route_for(None)`
    is empty. Same silence, same fix."""
    intents, _ = decide(FleetState(), [ready(9, "docs", klass=None)], POLICY, [], T0)
    assert Escalate(task=9, reason="unroutable") in intents


def test_an_unroutable_task_is_escalated_even_with_no_free_slots():
    """The escalation is a fact about the policy, not about capacity. Catches
    folding this into the dispatch loop, where `free <= 0` breaks out first and a
    full fleet hides the misconfiguration for exactly as long as it is busy."""
    small = dataclasses.replace(POLICY, slots=1)
    agents = [AgentState(task=1, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [live(), ready(9, "docs", klass="spec")], small, agents, T0)
    assert Escalate(task=9, reason="unroutable") in intents


def test_an_unroutable_task_that_is_not_auto_ok_is_left_alone():
    """auto-ok is a human's approval for unattended work. Its absence is why the
    task is sitting in READY, so waking a human about it is noise, not signal."""
    intents, _ = decide(
        FleetState(), [ready(9, "docs", klass="spec", auto_ok=False)], POLICY, [], T0
    )
    assert intents == []


def test_a_closed_unroutable_task_is_not_escalated():
    """A CLOSED issue is not a task, here as everywhere else."""
    closed = dataclasses.replace(ready(9, "docs", klass="spec"), is_open=False)
    intents, _ = decide(FleetState(), [closed], POLICY, [], T0)
    assert intents == []


def test_a_task_in_review_is_continued_to_the_reviewer():
    """route[1] of the patch route. Without this the task sits at fleet:review
    holding its area and its slot for the rest of the night, because REAP only
    touches CLAIMED and nothing else can move it."""
    intents, _ = decide(FleetState(), [staged(1, "review")], POLICY, [], T0)
    assert Continue(task=1, role="reviewer", base="integration") in intents


def test_a_continuation_never_emits_a_dispatch():
    """A Dispatch would relabel the task to CLAIMED. This is the assertion that
    catches the whole class: it fails the moment someone reuses Dispatch here
    because the two look interchangeable at the call site."""
    intents, _ = decide(FleetState(), [staged(1, "review")], POLICY, [], T0)
    assert dispatched(intents) == []


def test_a_task_in_integrating_is_continued_to_the_integrator():
    intents, _ = decide(FleetState(), [staged(1, "integrating")], POLICY, [], T0)
    assert Continue(task=1, role="integrator", base="integration") in intents


def test_a_continuation_is_not_charged_a_slot_it_already_holds():
    """The deadlock this pass exists to prevent. At slots=1 the reviewing task IS
    the one live task, so `free` is zero -- and a single-pass dispatch would
    refuse to advance the only task the fleet is working on."""
    small = dataclasses.replace(POLICY, slots=1)
    intents, _ = decide(FleetState(), [staged(1, "review")], small, [], T0)
    assert continued(intents) == [1]


def test_a_continuation_is_not_excluded_by_the_area_it_holds_itself():
    intents, _ = decide(FleetState(), [staged(1, "review", area="eval")], POLICY, [], T0)
    assert continued(intents) == [1]


def test_a_continuation_with_a_live_agent_is_not_started_twice():
    """The reviewer is already running. Starting again puts a second agent in
    the same worktree -- the collision `busy` exists to prevent."""
    agents = [AgentState(task=1, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [staged(1, "review")], POLICY, agents, T0)
    assert continued(intents) == []


def test_a_spec_task_that_reaches_review_escalates_rather_than_raising():
    """The `spec` route is (planner,), so route[1] does not exist. An IndexError
    here comes out of the pure core and takes every other task's intents with
    it; the escalation names the policy error instead."""
    spec = staged(9, "review", klass="spec")
    policy = dataclasses.replace(POLICY, routes=(("spec", ("planner",)),))
    intents, _ = decide(FleetState(), [spec], policy, [], T0)
    assert Escalate(task=9, reason="unroutable_stage") in intents
    assert continued(intents) == []


def test_a_one_role_route_never_sends_its_only_role_to_integrate():
    """route[-1] on a one-role route resolves to route[0]. Unguarded, a `spec`
    task at INTEGRATING would send a planner to merge the integration branch."""
    spec = staged(9, "integrating", klass="spec")
    policy = dataclasses.replace(POLICY, routes=(("spec", ("planner",)),))
    intents, _ = decide(FleetState(), [spec], policy, [], T0)
    assert continued(intents) == []
    assert Escalate(task=9, reason="unroutable_stage") in intents


def test_a_patch_task_at_integrating_takes_the_global_lock():
    """A class:patch task at INTEGRATING has an integrator writing the same
    branch a class:integration task would. Keying the lock on klass alone -- as
    M2 does -- lets a second writer onto `integration`. #8 is the assertion that
    matters: it is READY, in a free area, and must NOT go out."""
    intents, _ = decide(FleetState(), [staged(1, "integrating"), ready(8, "train")], POLICY, [], T0)
    assert continued(intents) == [1]
    assert dispatched(intents) == []


def test_two_tasks_at_integrating_start_one_integrator():
    intents, _ = decide(
        FleetState(),
        [staged(1, "integrating"), staged(2, "integrating", area="train")],
        POLICY,
        [],
        T0,
    )
    assert len(continued(intents)) == 1


def test_the_drain_stops_continuations_too():
    """Draining means no new agent starts, and a reviewer is a new agent. A
    drain that only gated READY would keep spending past the ceiling for as long
    as tasks kept reaching REVIEW."""
    state = FleetState(spent_usd=POLICY.fleet_usd)
    intents, _ = decide(state, [staged(1, "review")], POLICY, [], T0)
    assert intents == [Drain(reason="budget")]
