from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet import ledger
from fleet.gh import FakeGh
from fleet.labels import static_vocabulary

CAPTURED = Path(__file__).resolve().parents[1] / "fixtures/ledger/issues.json"


def row(number=1, labels=(), state="OPEN", title="t", body="", assignees=()):
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "labels": list(labels),
        "assignees": list(assignees),
    }


def test_label_objects_and_label_names_parse_identically():
    """Catches a parse that indexes label dicts directly, or one that assumes
    names. gh returns [{"id":..,"name":..}]; FakeGh stores ["fleet:ready"]. This
    is the divergence fixtures/ledger/README.md warns about, and ledger.read is
    the one place that has to absorb it."""
    as_objects = row(labels=[{"id": "x", "name": "fleet:ready"}, {"id": "y", "name": "area:eval"}])
    as_names = row(labels=["fleet:ready", "area:eval"])
    assert ledger.read_one(as_objects) == ledger.read_one(as_names)


def test_state_is_read_from_the_label_not_the_issue_state():
    """Catches reading `row["state"]` as the fleet state. That field is
    OPEN/CLOSED -- the issue's openness -- and conflating the two makes every
    open task look "ready"."""
    task = ledger.read_one(row(labels=["fleet:claimed"], state="OPEN"))
    assert task.state == "claimed"
    assert task.is_open is True


def test_the_other_fleet_labels_are_not_mistaken_for_states():
    """Catches the state parse regressing to a `fleet:` prefix test. auto-ok,
    attempt-N and fix-pass-N all carry that prefix without being states, so a
    prefix test raises `unknown state: 'auto-ok'` on 11 of the 13 captured rows
    -- i.e. on every task that is dispatchable or has been tried once."""
    task = ledger.read_one(
        row(labels=["fleet:claimed", "fleet:auto-ok", "fleet:attempt-2", "fleet:fix-pass-1"])
    )
    assert (task.state, task.auto_ok, task.attempts, task.fix_passes) == ("claimed", True, 2, 1)


def test_closed_is_matched_case_insensitively():
    """Catches `row["state"] == "CLOSED"`: gh's --state flag accepts any casing
    and FakeGh mirrors that, so a literal comparison diverges from both."""
    assert ledger.read_one(row(state="CLOSED")).is_open is False
    assert ledger.read_one(row(state="closed")).is_open is False


def test_a_task_with_no_state_label_reads_as_triage():
    """Catches raising on a state-less row. _post adds before it removes, but a
    human edit or an older CLI can still leave a task label-less (project-b
    #22, 2026-09-25), and the allocator must see it rather than KeyError on it."""
    assert ledger.read_one(row(labels=["area:eval"])).state == "triage"


def test_two_state_labels_are_rejected_loudly():
    """Catches a parse that silently picks the first of two. A task in both
    fleet:claimed and fleet:review is a torn write, and picking one at random
    means the tick's decision depends on gh's label ordering."""
    with pytest.raises(ledger.LedgerError, match="more than one state label"):
        ledger.read_one(row(labels=["fleet:claimed", "fleet:review"]))


def test_counters_take_the_highest_label_present():
    """Catches counting labels instead of reading them, and catches taking the
    first: attempt-1 is not removed when attempt-2 is added."""
    task = ledger.read_one(row(labels=["fleet:attempt-1", "fleet:attempt-2"]))
    assert task.attempts == 2


def test_counters_are_zero_when_absent():
    """Catches a counter that defaults to 1: a task on its first dispatch has no
    attempt label yet, and `attempts=1` there spends an attempt nobody used."""
    task = ledger.read_one(row(labels=["fleet:ready"]))
    assert task.attempts == 0
    assert task.fix_passes == 0


def test_auto_ok_is_read_from_its_own_label():
    """Catches inferring auto_ok from area or class. Spec §5 ruling 1: the label
    alone gates dispatch, never the area."""
    assert ledger.read_one(row(labels=["fleet:auto-ok"])).auto_ok is True
    assert ledger.read_one(row(labels=["area:configs"])).auto_ok is False


def test_area_and_class_are_read_from_their_prefixes():
    """Catches swapping the two prefixes, which would leave every task's area
    None and disable area exclusion entirely."""
    task = ledger.read_one(row(labels=["area:configs", "class:investigation"]))
    assert task.area == "configs"
    assert task.klass == "investigation"


def test_an_unknown_area_label_is_rejected_loudly():
    """Catches dropping the AREAS check. A typo'd area silently becomes an area
    no ownership rule covers, so the task is excluded from nothing and every
    agent piles into it."""
    with pytest.raises(ledger.LedgerError, match="unknown area"):
        ledger.read_one(row(labels=["area:evla"]))


def test_an_area_is_checked_against_the_areas_passed_in():
    """Catches read_one ignoring its `areas` argument and still using the
    fixture tuple: `allocator` is valid for the fleet repo and unknown to the
    fixture."""
    task = ledger.read_one(row(labels=["area:allocator"]), areas=frozenset({"allocator"}))
    assert task.area == "allocator"
    with pytest.raises(ledger.LedgerError, match="unknown area"):
        ledger.read_one(row(labels=["area:eval"]), areas=frozenset({"allocator"}))


def test_two_area_labels_are_rejected_loudly():
    """Catches a parse that validates all values but returns the first. Area
    exclusion holds ONE area per task, so a task in area:eval and area:data is
    excluded from one while its agent edits both."""
    with pytest.raises(ledger.LedgerError, match="more than one area"):
        ledger.read_one(row(labels=["area:eval", "area:data"]))


def test_the_captured_real_ledger_parses():
    """The whole point of capturing it. If this ever fails, the fake and the real
    client have diverged again. The auto_ok count is the load-bearing assertion:
    a `fleet:` prefix state parse raises on exactly those 11 rows."""
    tasks = ledger.read(json.loads(CAPTURED.read_text()))
    assert len(tasks) == 13
    assert sum(1 for t in tasks if t.auto_ok) == 11
    by_number = {t.number: t for t in tasks}
    assert by_number[1].state == "ready"
    assert by_number[1].area == "eval"
    assert by_number[1].klass == "patch"
    assert by_number[1].auto_ok is True
    assert by_number[1].attempts == 0
    assert by_number[10].klass == "investigation"
    assert by_number[12].state == "review"
    assert by_number[12].is_open is False
    assert by_number[13].state == "triage"


def test_fake_gh_output_parses_the_same_way():
    """Parity: the same assertions over the fake's shape. Catches a parse that
    only ever worked against the captured file."""
    gh = FakeGh()
    gh.create_issue("t", "b", ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"])
    task = ledger.read(gh.list_issues("all"))[0]
    assert (task.state, task.area, task.klass, task.auto_ok) == ("ready", "eval", "patch", True)


def test_the_assignee_is_read_from_gh_s_object_shape():
    """Catches storing the raw assignee object. Every captured row has an empty
    `assignees`, so nothing else in the suite exercises this branch -- and
    ownership lives in the assignee, so a dict there means no task ever matches
    its owner."""
    assert ledger.read_one(row(assignees=[{"login": "octocat"}])).assignee == "octocat"
    assert ledger.read_one(row(assignees=["octocat"])).assignee == "octocat"
    assert ledger.read_one(row()).assignee is None


def test_rebases_are_counted_from_labels():
    """The maximum N present, not the count -- rebase-1 is not removed when
    rebase-2 is added, exactly as attempts and fix passes accumulate."""
    task = ledger.read_one(
        {
            "number": 4,
            "title": "t",
            "state": "OPEN",
            "labels": ["fleet:claimed", "fleet:rebase-1", "fleet:rebase-2"],
        }
    )
    assert task.rebases == 2


def test_a_task_with_no_rebase_label_has_none():
    task = ledger.read_one({"number": 4, "title": "t", "state": "OPEN", "labels": ["fleet:ready"]})
    assert task.rebases == 0


def test_blocked_on_comes_from_the_packet_not_a_label():
    """Catches inventing a fleet:blocked-on-N label -- one gh has never heard of,
    so applying it would fail the whole edit. The dependency lives in the handoff
    packet."""
    task = ledger.read_one(
        row(labels=["fleet:blocked"]), packet={"to": "blocked", "blocked_on": 11}
    )
    assert task.blocked_on == 11
    assert ledger.read_one(row(labels=["fleet:blocked"])).blocked_on is None


def test_class_build_and_bug_parse():
    assert ledger.read_one(row(labels=["class:build"])).klass == "build"
    assert ledger.read_one(row(labels=["class:bug"])).klass == "bug"


def test_answered_label_parses_to_count():
    task = ledger.read_one(row(labels=["fleet:answered-1", "fleet:answered-2"]))
    assert task.answers == 2


def test_answered_label_is_highest_n():
    """Catches counting labels instead of taking the highest N."""
    assert ledger.read_one(row(labels=["fleet:answered-3"])).answers == 3


def test_no_answered_label_is_zero():
    assert ledger.read_one(row(labels=["fleet:ready"])).answers == 0


def test_answered_label_not_in_static_vocabulary():
    """The box writes `fleet:answered-N` and creates it when it does; the seed
    does not. Catches the answer counter added to the up-front label set."""
    assert not any(label.startswith("fleet:answered-") for label in static_vocabulary())
