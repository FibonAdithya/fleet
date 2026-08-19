from __future__ import annotations

import pytest

from fleet.executor import FakeExecutor
from fleet.model import Assign, CloseIssue, SetLabels, StartAgent, StopAgent
from test_decide_reap import POLICY

PACKET = {"to": "review", "head_sha": "a91c204"}


def executor() -> FakeExecutor:
    return FakeExecutor(per_task_usd=POLICY.per_task_usd)


def started(ex: FakeExecutor, task: int = 1) -> None:
    ex.apply([StartAgent(task=task, role="implementer", base="integration")])


def test_starting_an_agent_makes_it_visible_as_working():
    ex = executor()
    started(ex)
    assert [a.task for a in ex.agents()] == [1]
    assert ex.agents()[0].status == "working"


def test_a_working_agents_sequence_advances_each_tick():
    """Stall detection reads a moving sequence, so a healthy agent must move.
    A fake whose sequence never moved would make every agent look stalled."""
    ex = executor()
    started(ex)
    first = ex.agents()[0].change_seq
    ex.tick()
    assert ex.agents()[0].change_seq > first


def test_a_stalled_agent_stops_advancing_but_stays_working():
    """The exact shape of the failure: nominally running, producing nothing."""
    ex = executor()
    started(ex)
    ex.script(1, "stall")
    ex.tick()
    seq = ex.agents()[0].change_seq
    ex.script(1, "stall")
    ex.tick()
    assert ex.agents()[0].change_seq == seq
    assert ex.agents()[0].status == "working"


def test_a_scripted_outcome_lasts_exactly_one_tick():
    """A tick's outcomes are the complete scripting for that tick. Sticky
    scripting made `outcomes: {}` mean "keep the previous failure", so a
    scenario could never show an agent recovering -- and the tick that reads as
    a reset is the one nobody re-checks."""
    ex = executor()
    started(ex)
    ex.script(1, "stall")
    ex.tick()
    seq = ex.agents()[0].change_seq
    ex.tick()
    assert ex.agents()[0].change_seq > seq


def test_a_clean_exit_reports_done_and_hands_over_its_packet():
    """The packet is the whole point of the outcome: REAP advances a finished
    task to `packet["to"]` and emits nothing at all without one."""
    ex = executor()
    started(ex)
    ex.script(1, "clean_exit", packet=PACKET)
    ex.tick()
    assert ex.agents()[0].status == "done"
    assert ex.packets() == {1: PACKET}


def test_a_clean_exit_without_a_packet_is_refused():
    """Spec §6 names the outcome `clean_exit(packet)`. Scripted without one it
    produces a done agent the allocator can do nothing with, which is
    indistinguishable from a healthy tick and passes as one."""
    with pytest.raises(ValueError, match="packet"):
        executor().script(1, "clean_exit")


def test_a_packet_on_an_outcome_that_hands_nothing_over_is_refused():
    """A packet on a stall would be a scenario author believing a task advanced
    when it did not."""
    with pytest.raises(ValueError, match="hands over no packet"):
        executor().script(1, "stall", packet=PACKET)


def test_a_finished_agent_is_gone_by_the_next_tick():
    """A process that exited is not live. Left in place it holds a slot forever,
    so a task that finished could never be replaced and no simulation could get
    past a full fleet."""
    ex = executor()
    started(ex)
    ex.script(1, "clean_exit", packet=PACKET)
    ex.tick()
    assert [a.task for a in ex.agents()] == [1]
    ex.tick()
    assert ex.agents() == []


def test_a_dirty_exit_reports_dirty():
    ex = executor()
    started(ex)
    ex.script(1, "dirty_exit")
    ex.tick()
    assert ex.agents()[0].status == "dirty"


def test_a_vanished_agent_disappears_rather_than_reporting_a_status():
    """Ledger drift is the one failure the allocator reads as an absence: the
    ledger says CLAIMED and herdr has no agent. Modelled as a status it would be
    reaped by reason instead, which is a different rule and a different test."""
    ex = executor()
    started(ex)
    ex.script(1, "vanished")
    ex.tick()
    assert ex.agents() == []


def test_over_budget_accrues_cost_past_the_policys_ceiling():
    """Asserted against `policy.per_task_usd`, not a literal. The magic `3.0`
    only exceeded the ceiling because the fixture's happened to be 2.0."""
    ex = executor()
    started(ex)
    ex.script(1, "over_budget")
    ex.tick()
    assert ex.agents()[0].cost_usd > POLICY.per_task_usd


def test_a_healthy_agent_stays_under_the_ceiling_for_a_whole_scenario():
    """The drip must not trip the per-task budget on its own: an agent alive for
    twenty ticks reaching `over_budget` untouched would release healthy tasks in
    every long scenario and be read as a budget rule working."""
    ex = executor()
    started(ex)
    for _ in range(20):
        ex.tick()
    assert ex.agents()[0].cost_usd < POLICY.per_task_usd


def test_stopping_an_agent_removes_it():
    ex = executor()
    started(ex)
    ex.apply([StopAgent(task=1)])
    assert ex.agents() == []


def test_closing_an_issue_removes_any_lingering_live_agent():
    """Defense in depth, the same reasoning `_reap`'s own comment gives for
    retiring an agent on a row closed under the fleet: a live agent on a
    closed issue is not meant to exist, and if one still shows up as live it
    must not keep reporting cost every tick. `Advance` already stops the
    agent before it closes the issue, so this only bites if something ever
    applies `CloseIssue` on its own -- which is exactly why the fake must not
    silently ignore it and trust that ordering forever."""
    ex = executor()
    started(ex)
    ex.apply([CloseIssue(task=1)])
    assert ex.agents() == []


def test_closing_an_issue_drops_its_pending_packet_too():
    ex = executor()
    started(ex)
    ex.script(1, "clean_exit", packet=PACKET)
    ex.tick()
    ex.apply([CloseIssue(task=1)])
    assert ex.packets() == {}


def test_a_restarted_agent_carries_no_packet_from_its_last_attempt():
    """Otherwise a re-dispatched task advances on the packet of the attempt that
    failed -- the poison task would `Advance` on attempt 2 rather than escalate."""
    ex = executor()
    started(ex)
    ex.script(1, "clean_exit", packet=PACKET)
    ex.tick()
    ex.apply([StopAgent(task=1)])
    started(ex)
    assert ex.packets() == {}


def test_applied_effects_are_recorded_in_order():
    """Scenarios assert on what was written, so the record is the interface."""
    ex = executor()
    effects = [SetLabels(task=1, add=("fleet:ready",)), Assign(task=1, owner=None)]
    ex.apply(effects)
    assert ex.applied == effects


def test_an_unknown_outcome_is_refused():
    """A typo in a scenario file would otherwise silently script a healthy agent
    and the scenario would pass while proving nothing."""
    with pytest.raises(ValueError, match="unknown outcome"):
        executor().script(1, "explodes")
