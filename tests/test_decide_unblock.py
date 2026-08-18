from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from fleet.decide import decide
from fleet.model import Escalate, FleetState, Task, Unblock
from test_decide_reap import POLICY

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)


def blocked(number=4, on=11):
    return Task(
        number=number,
        title="t",
        state="blocked",
        area="models",
        klass="patch",
        auto_ok=True,
        blocked_on=on,
        packet={"to": "blocked", "blocked_on": on},
    )


def done(number=11):
    return Task(
        number=number,
        title="t",
        state="done",
        area="data",
        klass="patch",
        auto_ok=True,
    )


def test_a_blocked_task_whose_dependency_is_done_is_requeued():
    """Harness §4 issue 4: requeued unattended, no human. One of the four hard
    fails, and the mutation this catches is the phase being absent entirely."""
    intents, _ = decide(FleetState(), [blocked(), done()], POLICY, [], T0)
    assert Unblock(task=4) in intents


def test_a_blocked_task_whose_dependency_is_open_stays_blocked():
    """Catches an unblock that fires on the existence of a dependency rather
    than on its state, which requeues every blocked task on the first tick."""
    waiting = Task(
        number=11,
        title="t",
        state="claimed",
        area="data",
        klass="patch",
        auto_ok=True,
    )
    intents, _ = decide(FleetState(), [blocked(), waiting], POLICY, [], T0)
    assert Unblock(task=4) not in intents


def test_a_dependency_that_is_not_in_the_ledger_does_not_unblock():
    """A missing dependency is a reason to leave the task blocked, not to assume
    it finished. Catches an implementation that reads DONE as "not present among
    the open tasks", which requeues work whose prerequisite never landed."""
    intents, _ = decide(FleetState(), [blocked(on=99)], POLICY, [], T0)
    assert Unblock(task=4) not in intents


def test_a_blocked_task_with_no_dependency_recorded_does_not_unblock():
    """Catches the tolerant variant, `blocked_on is None or blocked_on in done`,
    which requeues a task nobody ever recorded a prerequisite for."""
    orphan = Task(
        number=4,
        title="t",
        state="blocked",
        area="models",
        klass="patch",
        auto_ok=True,
    )
    intents, _ = decide(FleetState(), [orphan], POLICY, [], T0)
    assert Unblock(task=4) not in intents


def test_a_closed_blocked_task_is_not_unblocked():
    """`fleet plan` reads `list_issues("open")` and the simulator seeds from a
    file that includes closed rows. If UNBLOCK acted on a closed row the two
    would disagree about the same ledger -- and the intent would relabel an
    issue nobody is going to work."""
    intents, _ = decide(
        FleetState(), [dataclasses.replace(blocked(), is_open=False), done()], POLICY, [], T0
    )
    assert Unblock(task=4) not in intents


def test_unblocking_wakes_no_human():
    """block and escalate are deliberately distinct. Catches an unblock
    implemented as an escalation: a morning queue would fill with tasks that
    could have resolved themselves at 3am."""
    intents, _ = decide(FleetState(), [blocked(), done()], POLICY, [], T0)
    assert not [i for i in intents if isinstance(i, Escalate)]


def test_a_closed_dependency_satisfies_unblock():
    """The ordinary end state of a completed task is CLOSED and fleet:done, and
    _open dropped it from the ledger before _unblock built its `done` set -- so
    the task it should release was never released. The fleet does not deadlock
    (a BLOCKED task holds no slot and no area); that one task simply never runs."""
    blocker = Task(
        number=2, title="t", state="done", area="data", klass="patch", auto_ok=True, is_open=False
    )
    waiting = Task(
        number=4,
        title="t",
        state="blocked",
        area="models",
        klass="patch",
        auto_ok=True,
        blocked_on=2,
        packet={"blocked_on": 2},
    )
    intents, _ = decide(FleetState(), [blocker, waiting], POLICY, [], T0)
    assert Unblock(task=4) in intents


def test_a_closed_dependency_satisfies_unblock_even_without_the_done_label():
    """A closed issue IS a satisfied dependency whatever label it still carries.
    Keying on the label alone leaves a task blocked forever behind an issue a
    human closed by hand."""
    blocker = Task(
        number=2, title="t", state="review", area="data", klass="patch", auto_ok=True, is_open=False
    )
    waiting = Task(
        number=4,
        title="t",
        state="blocked",
        area="models",
        klass="patch",
        auto_ok=True,
        blocked_on=2,
        packet={"blocked_on": 2},
    )
    intents, _ = decide(FleetState(), [blocker, waiting], POLICY, [], T0)
    assert Unblock(task=4) in intents
