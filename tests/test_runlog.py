from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from fleet import runlog
from fleet.model import Dispatch, Release, Unblock
from fleet.runlog import RunLog, read

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)


def dispatch(task=1, attempt=1):
    return Dispatch(task=task, role="implementer", base="integration", attempt=attempt)


def test_each_entry_is_one_json_line(tmp_path):
    """One line per entry, not one array per run: a night that dies at 4am must
    leave a parseable prefix rather than a truncated `[`."""
    log = RunLog(tmp_path / "run.jsonl", run_id="r1")
    log.append(tick=1, at=T0, intent=dispatch(), outcome="applied")
    log.append(
        tick=2, at=T0, intent=Release(task=1, reason="stalled", attempt=2), outcome="applied"
    )
    lines = (tmp_path / "run.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["intent"]["kind"] == "Dispatch"


def test_every_field_of_the_intent_is_recorded_not_just_its_name(tmp_path):
    """The attempt number is the field the poison-task and stall claims turn on.
    A log that recorded only the kind could not answer "which attempt was that"
    after the labels moved on."""
    log = RunLog(tmp_path / "run.jsonl", run_id="r1")
    log.append(
        tick=1, at=T0, intent=Release(task=4, reason="stalled", attempt=2), outcome="applied"
    )
    entry = read(tmp_path / "run.jsonl")[0]
    assert entry["intent"] == {"kind": "Release", "task": 4, "reason": "stalled", "attempt": 2}
    assert entry["at"] == T0.isoformat()


def test_a_transient_label_survives_in_the_log(tmp_path):
    """m2-inputs §3: rubric 04 asserts fleet:blocked was applied, but the next
    transition removes it, so the final ledger cannot answer "was it ever
    applied". Only this file can -- and only if it appends per event rather than
    keeping the latest entry per task, which is the same erasure one level up."""
    log = RunLog(tmp_path / "run.jsonl", run_id="r1")
    log.append(tick=1, at=T0, intent=Unblock(task=4), outcome="applied")
    log.append(tick=2, at=T0, intent=dispatch(task=4), outcome="applied")
    kinds = [e["intent"]["kind"] for e in read(tmp_path / "run.jsonl") if e["intent"]["task"] == 4]
    assert kinds == ["Unblock", "Dispatch"]


def test_appending_never_truncates_an_existing_log(tmp_path):
    """A run that dies mid-night must leave everything up to that point intact."""
    path = tmp_path / "run.jsonl"
    RunLog(path, run_id="r1").append(tick=1, at=T0, intent=Unblock(task=4), outcome="applied")
    RunLog(path, run_id="r1").append(tick=2, at=T0, intent=Unblock(task=5), outcome="applied")
    assert len(read(path)) == 2


def test_the_run_id_is_on_every_entry(tmp_path):
    log = RunLog(tmp_path / "run.jsonl", run_id="night-3")
    log.append(tick=1, at=T0, intent=Unblock(task=4), outcome="applied")
    assert read(tmp_path / "run.jsonl")[0]["run_id"] == "night-3"


def test_a_reaping_entry_records_the_cost_it_finalised(tmp_path):
    log = RunLog(tmp_path / "run.jsonl", run_id="r1")
    log.append(
        tick=1,
        at=T0,
        intent=Release(task=5, reason="stalled", attempt=2),
        outcome="applied",
        cost_usd=1.75,
    )
    assert runlog.read(tmp_path / "run.jsonl")[0]["cost_usd"] == 1.75


def test_a_zero_cost_entry_is_recorded_explicitly_not_omitted(tmp_path):
    """0.0 is a legitimate cost -- a free tick, an agent that did nothing, a
    model priced at zero. The key must be present and equal to 0.0, not
    silently dropped, so a mixed log can tell 'costed nothing' apart from
    'this entry predates cost tracking' (see the missing-key test below)."""
    log = RunLog(tmp_path / "run.jsonl", run_id="r1")
    log.append(
        tick=1,
        at=T0,
        intent=Release(task=5, reason="stalled", attempt=2),
        outcome="applied",
        cost_usd=0.0,
    )
    entry = read(tmp_path / "run.jsonl")[0]
    assert "cost_usd" in entry
    assert entry["cost_usd"] == 0.0


def test_spent_is_rebuilt_by_summing_the_log(tmp_path):
    """A restarted daemon whose spent_usd resets to zero hands the fleet a
    second full budget, and the drain rule never fires. The daemon is designed
    to be restartable, so this is the documented recovery path, not an edge."""
    path = tmp_path / "run.jsonl"
    log = RunLog(path, run_id="r1")
    log.append(
        tick=1,
        at=T0,
        intent=Release(task=5, reason="stalled", attempt=2),
        outcome="applied",
        cost_usd=1.5,
    )
    log.append(
        tick=2,
        at=T0,
        intent=Dispatch(task=6, role="implementer", base="integration", attempt=1),
        outcome="applied",
    )
    log.append(
        tick=3,
        at=T0,
        intent=Release(task=6, reason="dirty_exit", attempt=2),
        outcome="applied",
        cost_usd=2.25,
    )
    assert runlog.spent_from_log(path) == pytest.approx(3.75)


def test_spent_from_a_missing_log_is_zero(tmp_path):
    """First run of a fresh fleet. Raising would make the daemon unable to start
    without a log it has not written yet."""
    assert runlog.spent_from_log(tmp_path / "absent.jsonl") == 0.0


def test_spent_ignores_entries_from_other_runs(tmp_path):
    """One log file can hold several nights. Summing all of them would start
    tonight already drained."""
    path = tmp_path / "run.jsonl"
    RunLog(path, run_id="old").append(
        tick=1,
        at=T0,
        intent=Release(task=5, reason="stalled", attempt=2),
        outcome="applied",
        cost_usd=9.0,
    )
    RunLog(path, run_id="new").append(
        tick=1,
        at=T0,
        intent=Release(task=6, reason="stalled", attempt=2),
        outcome="applied",
        cost_usd=1.0,
    )
    assert runlog.spent_from_log(path, run_id="new") == pytest.approx(1.0)


def test_a_truncated_final_line_does_not_break_recovery(tmp_path):
    """A daemon killed mid-write leaves a partial line, and the kill is the
    exact event this function exists to recover from."""
    path = tmp_path / "run.jsonl"
    RunLog(path, run_id="r1").append(
        tick=1,
        at=T0,
        intent=Release(task=5, reason="stalled", attempt=2),
        outcome="applied",
        cost_usd=2.0,
    )
    with path.open("a") as handle:
        handle.write('{"run_id": "r1", "cost_us')
    assert runlog.spent_from_log(path) == pytest.approx(2.0)


def test_a_pre_task14_entry_with_no_cost_usd_key_costs_nothing_it_cannot_recall(tmp_path):
    """Entries written before this change carry no cost_usd field at all --
    not a zero, an absence. There is no way to recover what an old entry
    actually cost (the log format did not capture it), and raising here would
    make the daemon unable to restart against a log that predates this
    change -- exactly the log the M3 gate is likely to hand it. So a missing
    key contributes 0.0, same as an absent file, and is documented as such:
    this is silent underestimation of true historical spend, accepted because
    the alternative breaks every restart against a pre-existing log."""
    path = tmp_path / "run.jsonl"
    legacy_entry = {
        "run_id": "r1",
        "tick": 1,
        "at": T0.isoformat(),
        "intent": {"kind": "Release", "task": 5, "reason": "stalled", "attempt": 2},
        "outcome": "applied",
        # no "cost_usd" key at all -- this is what Task 1-13's RunLog wrote.
    }
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy_entry) + "\n")
    RunLog(path, run_id="r1").append(
        tick=2,
        at=T0,
        intent=Release(task=6, reason="dirty_exit", attempt=2),
        outcome="applied",
        cost_usd=4.0,
    )
    assert runlog.spent_from_log(path) == pytest.approx(4.0)


def test_summing_hundreds_of_entries_does_not_drift(tmp_path):
    """spent_from_log must be safe to call every tick against a long-running
    night. Naive float accumulation over hundreds of small costs should still
    land within floating-point tolerance of the exact decimal sum."""
    path = tmp_path / "run.jsonl"
    log = RunLog(path, run_id="r1")
    for i in range(500):
        log.append(
            tick=i,
            at=T0,
            intent=Release(task=5, reason="stalled", attempt=2),
            outcome="applied",
            cost_usd=0.01,
        )
    assert runlog.spent_from_log(path) == pytest.approx(5.0, abs=1e-6)


# ---------------------------------------------------------- rejoining a night


def test_the_latest_run_id_is_the_last_night_written_not_the_first(tmp_path):
    """One file holds several nights. A restart rejoins the one that was being
    written when the process died, so the LAST entry answers -- reading the
    first would resume a night that finished days ago and start already
    drained."""
    path = tmp_path / "run.jsonl"
    RunLog(path, run_id="night-1").append(tick=1, at=T0, intent=dispatch(), outcome="applied")
    RunLog(path, run_id="night-2").append(tick=1, at=T0, intent=dispatch(), outcome="applied")
    assert runlog.latest_run_id(path) == "night-2"


def test_the_latest_run_id_of_a_missing_log_is_none(tmp_path):
    """A fresh fleet has written nothing. None is the signal to mint an id, not
    an error that stops the daemon starting."""
    assert runlog.latest_run_id(tmp_path / "nothing.jsonl") is None


def test_the_latest_run_id_of_an_empty_log_is_none(tmp_path):
    """An existing but empty file is the same fact as a missing one. `""` is not
    a run id, and returning it would make `spent_from_log` filter on a value no
    entry carries -- the very defect resuming exists to close."""
    path = tmp_path / "run.jsonl"
    path.write_text("")
    assert runlog.latest_run_id(path) is None


def test_the_latest_run_id_survives_a_truncated_final_line(tmp_path):
    """The shape a `kill -9` mid-write leaves, which is precisely the event a
    restart recovers from. The half-written line is skipped and the last WHOLE
    entry answers -- not None, which would mint a fresh id and forget the
    night's spend on the one path this function exists to serve."""
    path = tmp_path / "run.jsonl"
    RunLog(path, run_id="tonight").append(tick=1, at=T0, intent=dispatch(), outcome="applied")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"run_id": "tonig')
    assert runlog.latest_run_id(path) == "tonight"


def test_an_entry_with_no_usable_run_id_does_not_answer(tmp_path):
    """A row whose run_id is absent, empty or not a string cannot name a night.
    Answering with it would hand `spent_from_log` a filter matching nothing,
    which reads as `$0.00 spent` -- a legal number and the wrong one."""
    path = tmp_path / "run.jsonl"
    RunLog(path, run_id="tonight").append(tick=1, at=T0, intent=dispatch(), outcome="applied")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"run_id": "", "cost_usd": 1.0}) + "\n")
        handle.write(json.dumps({"run_id": None, "cost_usd": 1.0}) + "\n")
        handle.write(json.dumps({"cost_usd": 1.0}) + "\n")
    assert runlog.latest_run_id(path) == "tonight"
