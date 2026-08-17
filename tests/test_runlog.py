from __future__ import annotations

import json
from datetime import datetime, timezone

from fleet.model import Dispatch, Release, Unblock
from fleet.runlog import RunLog, read

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)


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
