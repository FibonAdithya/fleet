from __future__ import annotations

import json
from pathlib import Path

from fleet.gh import FakeGh
from fleet.reader import PACKET_STATES, read_ledger

CAPTURE = Path(__file__).resolve().parents[1] / "fixtures/ledger/issue-comments.json"


def test_the_query_is_all_not_open():
    """Once the shell fetches packets, blocked_on is populated and UNBLOCK
    becomes reachable -- and an open-only query drops the closed dependency that
    would satisfy it, reintroducing from the query side exactly the wedge the
    filter split removes. The two halves are one fix."""
    seen = []
    gh = FakeGh()
    gh.create_issue("t", "b", ["fleet:ready", "area:eval"])
    inner = gh.list_issues
    gh.list_issues = lambda state="open": (seen.append(state), inner(state))[1]
    read_ledger(gh)
    assert seen == ["all"]


def test_packets_are_fetched_only_for_states_that_carry_one():
    """`gh issue view` is one API call per issue. Fetching for every row turns
    one list call into fifty, and secondary rate limits are real."""
    gh = FakeGh()
    ready = gh.create_issue("t", "b", ["fleet:ready", "area:eval"])
    claimed = gh.create_issue("t", "b", ["fleet:claimed", "area:train"])
    fetched = []
    inner = gh.get_issue
    gh.get_issue = lambda n: (fetched.append(n), inner(n))[1]
    read_ledger(gh)
    assert claimed in fetched
    assert ready not in fetched


def test_a_blocked_task_gets_its_blocked_on_from_its_packet():
    """UNBLOCK is unreachable without this: do_plan passed no packets at all, so
    every Task.blocked_on was None."""
    from fleet import packet as packet_module

    gh = FakeGh()
    number = gh.create_issue("t", "b", ["fleet:blocked", "area:models"])
    gh.comment(
        number,
        packet_module.render(
            {
                "schema": "fleet.handoff.v1",
                "task": number,
                "to": "blocked",
                "blocked_on": 11,
                "attempt": 1,
            }
        ),
    )
    tasks = {t.number: t for t in read_ledger(gh)}
    assert tasks[number].blocked_on == 11


def test_closed_rows_are_returned_rather_than_filtered_here():
    """The filter belongs in decide(), where three phases disagree about what a
    closed row means. Filtering in the reader would take that choice away."""
    gh = FakeGh()
    number = gh.create_issue("t", "b", ["fleet:done", "area:eval"])
    gh.close_issue(number)
    assert [t.number for t in read_ledger(gh)] == [number]


def test_packets_are_fetched_for_review_and_integrating_too():
    """`decide._reap`'s `_is_rebase_handback` reads `task.packet` for any row in
    claimed, review or integrating before it branches on which one -- see the
    call-site enumeration in `reader.PACKET_STATES`'s comment. Both must be
    fetched to match that call site, or an integrator's handback packet posted
    while the label still reads REVIEW would silently read as None."""
    gh = FakeGh()
    review = gh.create_issue("t", "b", ["fleet:review", "area:eval"])
    integrating = gh.create_issue("t", "b", ["fleet:integrating", "area:eval"])
    fetched = []
    inner = gh.get_issue
    gh.get_issue = lambda n: (fetched.append(n), inner(n))[1]
    read_ledger(gh)
    assert review in fetched
    assert integrating in fetched


def test_packet_states_is_exactly_the_four_states_the_core_reads_packets_in():
    """Locks the set against accidental drift. `reader.py`'s comment above
    `PACKET_STATES` enumerates the call sites this is checked against:
    `decide._advance` (claimed) and `decide._is_rebase_handback` (claimed,
    review, integrating) both read `task.packet`; `ledger.read_one` reads it for
    `blocked_on`, which only ever arrives on a `blocked` packet."""
    assert PACKET_STATES == ("claimed", "review", "integrating", "blocked")


def test_the_captured_issue_parses_into_a_task_with_its_packet():
    """The fidelity obligation. gh returns label and assignee OBJECTS where
    FakeGh stores names, and comment objects where FakeGh stores bodies -- four
    such divergences did all the damage in M1."""
    raw = json.loads(CAPTURE.read_text())
    assert "comments" in raw
    assert isinstance(raw["comments"], list)
