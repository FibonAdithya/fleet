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


def test_the_captured_packet_recovers_through_parse_latest_with_no_attempt():
    """The fidelity obligation, exercised through the real path rather than
    just checked for well-formed JSON. gh returns comment OBJECTS
    (`{"body": ..., ...}`) where `FakeGh` stores bodies directly -- one of the
    four shape divergences that did all the damage in M1 -- so this feeds the
    captured comments' bodies through `packet.parse_latest` exactly as
    `reader.read_ledger` does, and asserts on the packet that comes back.

    `to == "review"` pins that the capture is not just parseable JSON but a
    real handoff this repo would act on. The missing-`attempt` assertion pins
    the capture's age: it was taken before Task 7 added `attempt` to the
    handoff schema, so a reader of this fixture must not mistake it for a
    current-format packet. The reader itself does not care -- it carries
    whatever `parse_latest` returns straight through -- but `decide._advance`
    does: `task.packet.get("attempt") != max(task.attempts, 1)` treats a
    missing `attempt` as a mismatch and refuses to act on it. That boundary
    -- the reader carries an attempt-less packet through untouched, and only
    the core refuses to advance on it -- is the fact this capture documents.
    """
    from fleet import packet as packet_module

    raw = json.loads(CAPTURE.read_text())
    bodies = [c["body"] for c in raw["comments"]]
    recovered = packet_module.parse_latest(bodies, "handoff")
    assert recovered is not None
    assert recovered["to"] == "review"
    assert "attempt" not in recovered


def test_read_ledger_marks_the_owners_issues_and_reads_their_age():
    """ADOPT needs three facts the label set does not carry: who wrote the
    issue, when, and whether it has any state label at all. `by_owner` is True
    only for the login the daemon runs as; with no login, nobody is the owner."""
    from datetime import UTC, datetime

    gh = FakeGh(clock=lambda: datetime(2026, 11, 16, 9, 30, tzinfo=UTC))
    # The box creates this label in the target repo; it is not fleet's.
    gh.ensure_label("agent-reported")
    mine = gh.create_issue("mine", "b", ["agent-reported"], author="FibonAdithya")
    theirs = gh.create_issue("theirs", "b", ["agent-reported"], author="someone-else")
    triaged = gh.create_issue("triaged", "b", ["agent-reported", "fleet:triage"])
    tasks = {t.number: t for t in read_ledger(gh, login="FibonAdithya")}
    # R11: both read as triage; only the label tells them apart.
    assert tasks[triaged].state == tasks[mine].state == "triage"
    assert tasks[triaged].has_state_label is True
    assert tasks[mine].by_owner is True
    assert tasks[theirs].by_owner is False
    assert tasks[mine].agent_reported is True
    assert tasks[mine].has_state_label is False
    assert tasks[mine].created_at == datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
    assert all(not t.by_owner for t in read_ledger(gh))


def test_captured_rows_without_author_or_created_at_are_never_the_owners():
    """fixtures/ledger was captured before `author,createdAt` were fetched and is
    not re-captured. A missing author is not the owner and a missing createdAt
    has no age, so neither row can be adopted."""
    from fleet import ledger as ledger_module

    rows = json.loads((CAPTURE.parent / "issues.json").read_text())
    tasks = ledger_module.read(rows, login="FibonAdithya")
    assert tasks and all(not t.by_owner and t.created_at is None for t in tasks)
    # No login and no author are both None; None is nobody, not a match.
    assert all(not t.by_owner for t in ledger_module.read(rows))
