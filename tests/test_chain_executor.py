from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fleet import chain_executor, escalation, labels, packet
from fleet.chain_executor import ChainExecutor, ChainRun, ChainStateError, parse_status
from fleet.decide import decide
from fleet.expand import expand
from fleet.gh import FakeGh, GhError
from fleet.model import (
    AgentState,
    Comment,
    Continue,
    Escalate,
    FleetState,
    Observation,
    Policy,
    Release,
    Retire,
    StartAgent,
    StopAgent,
    Task,
)
from fleet.schemas import validate

FIXTURE = Path(__file__).resolve().parent / "fixtures/chain/status-v1.json"
COMMAND = "./bin/chain"
SPEC_BODY = "## Goal\n\nBuild the thing.\n\nSpec: docs/ai/specs/x.md\n"
T0 = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)
CHAIN_POLICY = Policy(
    areas=(("src/eval/**", "eval"),),
    routes=(("build", ("chain",)),),
    agents=(("chain", "chain", "chain"),),
    integration_branch="integration",
    slots=6,
    stall_minutes=10,
    max_attempts=2,
    max_fix_passes=1,
    max_rebases=1,
    per_task_usd=2.0,
    fleet_usd=25.0,
    ceilings=(("chain", 40.0),),
)


class ScriptedCli:
    """A fake `run_cli`: answers each chain subcommand from a script and records
    every argv it was given.

    Never more permissive than the real binary: a subcommand with nothing
    scripted answers the way an unknown invocation does (exit 2, no stdout), so
    a test that forgets to script a call sees a failure, not a success."""

    def __init__(self, **script: list[tuple[int, str, str]]) -> None:
        self.script = {name: list(answers) for name, answers in script.items()}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        answers = self.script.get(argv[1], [])
        if not answers:
            return (2, "", f"chain: no scripted answer for {argv[1]}")
        # The last answer repeats, so a status scripted once answers every tick.
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def calls_to(self, sub: str) -> list[list[str]]:
        return [argv for argv in self.calls if argv[1] == sub]


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _status(*runs: dict) -> str:
    return json.dumps({"schema": "chain.status.v1", "runs": list(runs)})


def _run(run_id: str, number: int, status: str = "running", slug: str = "o/r", **kw) -> dict:
    """One status object, built from the fixture's running run so every field
    the contract carries is present."""
    run = copy.deepcopy(_fixture()["runs"][0])
    run["id"] = run_id
    run["fleet_task"] = {"slug": slug, "number": number, "kind": "build"}
    run["status"] = status
    run.update(kw)
    return run


def _issue_at(gh: FakeGh, number: int, klass: str = "build", body: str = SPEC_BODY) -> int:
    """Create filler issues until the next one is `number`, so argv carries a
    task number that cannot be the fake's default first issue by accident."""
    while gh._next < number:
        gh.create_issue("filler", "", ["fleet:triage"])
    created = gh.create_issue("Build it", body, ["fleet:claimed", f"class:{klass}", "area:eval"])
    assert created == number
    return created


@pytest.fixture()
def ccfg(cfg, tmp_path):
    # Never the real ~/.local/state answers directory.
    return dataclasses.replace(cfg, answers_dir=tmp_path / "answers")


def _executor(ccfg, gh, cli, state_path=None, **kw):
    path = state_path or ccfg.root / "state" / "chain-runs.json"
    return ChainExecutor(ccfg, gh, COMMAND, cli, path, **kw)


# --- parse_status ---------------------------------------------------------------


def test_parse_status_fixture():
    runs = parse_status(FIXTURE.read_text())
    assert [r.id for r in runs] == ["r-g", "r-f", "r-e", "r-d", "r-c", "r-b", "r-a"]
    assert all(isinstance(r, ChainRun) for r in runs)
    assert [r.status for r in runs] == [
        "running",
        "paused",
        "approved",
        "ended",
        "ended",
        "ended",
        "aborted",
    ]
    first = runs[0]
    assert first.fleet_task.slug == "acme/widgets"
    assert first.fleet_task.number == 7
    assert first.fleet_task.kind == "build"
    assert first.progress == 3
    assert first.output_bytes == 0
    assert first.run_spent_usd == 1.25
    assert runs[1].paused_until == 5000.0
    assert runs[2].outcome == "approved"
    assert runs[2].pr == {"number": 42, "url": "https://github.com/acme/widgets/pull/42"}
    assert runs[3].escalation["options"] == ["sqlite", "postgres"]
    # A fleet-mode escalation ends with status `ended`; the outcome says why.
    assert (runs[3].status, runs[3].outcome) == ("ended", "escalated")
    assert runs[4].verdict["tried"] == ["ran the repro twice"]
    assert runs[2].stage == "merge"
    assert {r.base for r in runs} == {"integration"}
    assert runs[5].verdict["duplicate_of"] == 12
    assert runs[2].test_command == "make check"


def test_parse_status_rejects_other_schema():
    doc = _fixture()
    doc["schema"] = "chain.status.v2"
    with pytest.raises(ValueError):
        parse_status(json.dumps(doc))
    del doc["schema"]
    with pytest.raises(ValueError):
        parse_status(json.dumps(doc))


def test_parse_status_rejects_a_run_missing_a_contract_field():
    doc = _fixture()
    del doc["runs"][0]["progress"]
    with pytest.raises(ValueError):
        parse_status(json.dumps(doc))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("id",), ["r-g"]),
        (("id",), 7),
        (("status",), {"s": "running"}),
        (("fleet_task", "slug"), 1),
        (("fleet_task", "number"), "7"),
        (("fleet_task", "number"), True),
        (("progress",), "3"),
        (("progress",), 3.5),
        (("progress",), True),
        (("output_bytes",), None),
        (("run_spent_usd",), "1.25"),
        (("run_spent_usd",), True),
        (("run_spent_usd",), float("nan")),
        (("run_spent_usd",), float("inf")),
        (("run_spent_usd",), -0.5),
        (("paused_until",), "5000"),
        (("paused_until",), {"at": 5000}),
        (("paused_until",), True),
        (("paused_until",), float("nan")),
        (("fleet_task", "kind"), "patch"),
        (("fleet_task", "kind"), None),
    ],
    ids=lambda v: repr(v),
)
def test_parse_status_rejects_wrong_field_types(path, value):
    """A wrong-typed field raises ValueError, which agents() turns into "keep
    the last good read". A TypeError would escape the tick every time, and an
    int `id` would silently never match the task map."""
    doc = _fixture()
    target = doc["runs"][0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        parse_status(json.dumps(doc))


def test_parse_status_rejects_bad_json():
    with pytest.raises(ValueError):
        parse_status("not json")


# --- start ----------------------------------------------------------------------


def test_start_build_passes_spec_and_base(ccfg):
    gh = FakeGh()
    _issue_at(gh, 5)
    cli = ScriptedCli(start=[(0, "r-g\n", "")])
    ex = _executor(ccfg, gh, cli)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert cli.calls_to("start") == [
        [
            COMMAND,
            "start",
            "--fleet",
            "o/r#5",
            "--kind",
            "build",
            "--base",
            "integration",
            str(ccfg.root),
            "docs/ai/specs/x.md",
        ]
    ]
    assert ex.tasks() == {5}
    assert list(ex.failures) == []


def test_start_does_not_assign_the_issue(ccfg):
    """Plan 3 reads an assigned fleet:human issue as taken by the owner."""
    gh = FakeGh()
    _issue_at(gh, 5)
    ex = _executor(ccfg, gh, ScriptedCli(start=[(0, "r-g\n", "")]))
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert gh.get_issue(5).get("assignees", []) == []


def test_start_bug_has_no_spec(ccfg):
    gh = FakeGh()
    _issue_at(gh, 5, klass="bug", body="It crashes.\n\nSpec: docs/ai/specs/x.md\n")
    cli = ScriptedCli(start=[(0, "r-b\n", "")])
    ex = _executor(ccfg, gh, cli)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert cli.calls_to("start") == [
        [
            COMMAND,
            "start",
            "--fleet",
            "o/r#5",
            "--kind",
            "bug",
            "--base",
            "integration",
            str(ccfg.root),
        ]
    ]


def test_start_passes_answers_when_file_exists(ccfg):
    gh = FakeGh()
    _issue_at(gh, 5)
    answers = ccfg.answers_dir / "o__r" / "5.json"
    cli = ScriptedCli(
        start=[(0, "r-1\n", ""), (0, "r-2\n", "")],
        status=[(0, _status(_run("r-1", 5, status="ended")), "")],
    )
    ex = _executor(ccfg, gh, cli)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert "--answers" not in cli.calls_to("start")[0]
    ex.agents()  # the first run has ended, so a second start replaces it
    answers.parent.mkdir(parents=True)
    answers.write_text(json.dumps([{"text": "sqlite", "at": 1.0}]))
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    argv = cli.calls_to("start")[1]
    assert argv[argv.index("--answers") + 1] == str(answers)
    # Options before the positionals, as chain's grammar reads them.
    assert argv[-2:] == [str(ccfg.root), "docs/ai/specs/x.md"]


def test_start_build_without_spec_line_starts_nothing(ccfg):
    """R14: the failed start is reported as a dirty agent, so decide's
    `dirty_exit` counts it toward the attempt cap and escalates at it."""
    gh = FakeGh()
    # A `Spec:` that is not a whole line does not count.
    _issue_at(gh, 5, body="No spec here.\nSee Spec: docs/x.md for details\n")
    cli = ScriptedCli(start=[(0, "r-g\n", "")], status=[(0, _status(), "")])
    ex = _executor(ccfg, gh, cli)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert cli.calls_to("start") == []
    assert len(ex.failures) == 1
    assert "5" in ex.failures[0]
    assert ex.agents() == [AgentState(task=5, status="dirty", change_seq=0)]
    assert ex.tasks() == {5}
    # The StopAgent that decide's release produces forgets it, aborting nothing.
    ex.apply([StopAgent(task=5)])
    assert cli.calls_to("abort") == []
    assert ex.tasks() == set()
    assert ex.agents() == []


def test_spec_line_with_crlf_body_is_found(ccfg):
    """GitHub stores issue bodies typed in the web UI with CRLF line ends."""
    gh = FakeGh()
    _issue_at(gh, 5, body="Goal\r\nSpec: docs/ai/specs/x.md\r\nmore\r\n")
    cli = ScriptedCli(start=[(0, "r-g\n", "")])
    _executor(ccfg, gh, cli).apply([StartAgent(task=5, role="chain", base="integration")])
    assert cli.calls_to("start")[0][-1] == "docs/ai/specs/x.md"


@pytest.mark.parametrize(
    "answer",
    [(1, "", "chain: refused"), (0, "", ""), (0, "\n  \n", "")],
    ids=["nonzero-exit", "empty-stdout", "blank-stdout"],
)
def test_failed_chain_start_reports_dirty(ccfg, answer):
    gh = FakeGh()
    _issue_at(gh, 5)
    cli = ScriptedCli(start=[answer], status=[(0, _status(), "")])
    ex = _executor(ccfg, gh, cli)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert len(ex.failures) == 1
    assert ex.agents() == [AgentState(task=5, status="dirty", change_seq=0)]
    assert ex.tasks() == {5}


def test_a_class_that_is_not_build_or_bug_starts_nothing(ccfg):
    gh = FakeGh()
    _issue_at(gh, 5, klass="patch")
    cli = ScriptedCli(start=[(0, "r-g\n", "")], status=[(0, _status(), "")])
    ex = _executor(ccfg, gh, cli)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert cli.calls_to("start") == []
    assert ex.agents() == [AgentState(task=5, status="dirty", change_seq=0)]


def test_start_reads_the_run_id_from_the_last_stdout_line(ccfg):
    gh = FakeGh()
    _issue_at(gh, 5)
    cli = ScriptedCli(
        start=[(0, "note: queued\nr-9\n\n", "")],
        status=[(0, _status(_run("r-9", 5)), "")],
    )
    ex = _executor(ccfg, gh, cli)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert [a.task for a in ex.agents()] == [5]


def test_a_missing_binary_is_a_recorded_failure_not_a_crash(ccfg):
    gh = FakeGh()
    _issue_at(gh, 5)

    def missing(argv):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    ex = _executor(ccfg, gh, missing)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert ex.agents() == [AgentState(task=5, status="dirty", change_seq=0)]
    assert len(ex.failures) == 2  # the start, then the status call


# --- stop -----------------------------------------------------------------------


def _started(ccfg, cli, task=5, state_path=None):
    gh = FakeGh()
    _issue_at(gh, task)
    ex = _executor(ccfg, gh, cli, state_path=state_path)
    ex.apply([StartAgent(task=task, role="chain", base="integration")])
    return ex


def test_stop_aborts_the_run(ccfg):
    cli = ScriptedCli(
        start=[(0, "r-g\n", "")],
        status=[(0, _status(_run("r-g", 5)), "")],
        abort=[(0, "abort requested for run r-g\n", "")],
    )
    ex = _started(ccfg, cli)
    ex.agents()
    ex.apply([StopAgent(task=5)])
    assert cli.calls_to("abort") == [[COMMAND, "abort", "r-g"]]
    assert list(ex.failures) == []


def test_stop_forgets_the_task(ccfg):
    cli = ScriptedCli(
        start=[(0, "r-g\n", "")],
        status=[(0, _status(_run("r-g", 5)), "")],
        abort=[(0, "abort requested for run r-g\n", "")],
    )
    ex = _started(ccfg, cli)
    assert [a.task for a in ex.agents()] == [5]
    ex.apply([StopAgent(task=5)])
    assert ex.tasks() == set()
    # chain applies the abort on its next tick, so status still lists it running.
    assert ex.agents() == []


def test_stop_of_a_run_known_terminal_does_not_abort(ccfg):
    """chain refuses `abort` on an ended run with exit 2."""
    cli = ScriptedCli(
        start=[(0, "r-g\n", "")],
        status=[(0, _status(_run("r-g", 5, status="ended")), "")],
        abort=[(2, "", "chain: run r-g has ended (ended)")],
    )
    ex = _started(ccfg, cli)
    ex.agents()
    ex.apply([StopAgent(task=5)])
    assert cli.calls_to("abort") == []
    assert ex.tasks() == set()
    assert list(ex.failures) == []


def test_failed_abort_is_recorded_and_still_forgets(ccfg):
    cli = ScriptedCli(
        start=[(0, "r-g\n", "")],
        status=[(0, _status(_run("r-g", 5)), "")],
        abort=[(1, "", "chain: no such run")],
    )
    ex = _started(ccfg, cli)
    ex.apply([StopAgent(task=5)])
    assert len(ex.failures) == 1
    assert "r-g" in ex.failures[0]
    assert ex.tasks() == set()


def test_start_on_live_mapped_run_keeps_it(ccfg):
    """chain has no idempotence for a repeated `chain start --fleet`: a second
    StartAgent on a task whose run is live starts nothing, aborts nothing and
    keeps the mapping, and says so in a failure line."""
    cli = ScriptedCli(
        start=[(0, "r-1\n", ""), (0, "r-2\n", "")],
        status=[(0, _status(_run("r-1", 5)), "")],
        abort=[(0, "ok\n", "")],
    )
    ex = _started(ccfg, cli)
    ex.agents()
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert len(cli.calls_to("start")) == 1
    assert cli.calls_to("abort") == []
    assert ex._runs == {5: "r-1"}
    assert len(ex.failures) == 1
    assert "r-1" in ex.failures[0]
    assert [(a.task, a.status) for a in ex.agents()] == [(5, "working")]


def test_start_on_mapped_run_of_unknown_status_keeps_it(ccfg):
    """Before the first good status read (a restart into a chain outage) the
    mapped run may be live, so it is kept too."""
    state_path = ccfg.root / "state" / "chain-runs.json"
    cli = ScriptedCli(start=[(0, "r-1\n", ""), (0, "r-2\n", "")], status=[(1, "", "boom")])
    _started(ccfg, cli, state_path=state_path)
    gh = FakeGh()
    _issue_at(gh, 5)
    reborn = _executor(ccfg, gh, cli, state_path=state_path)
    reborn.apply([StartAgent(task=5, role="chain", base="integration")])
    assert len(cli.calls_to("start")) == 1
    assert cli.calls_to("abort") == []
    assert reborn._runs == {5: "r-1"}


def test_start_over_a_terminal_mapped_run_does_not_abort(ccfg):
    cli = ScriptedCli(
        start=[(0, "r-1\n", ""), (0, "r-2\n", "")],
        status=[(0, _status(_run("r-1", 5, status="ended")), "")],
    )
    ex = _started(ccfg, cli)
    ex.agents()
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert cli.calls_to("abort") == []
    assert ex._runs == {5: "r-2"}


def test_a_successful_start_clears_a_failed_start(ccfg):
    gh = FakeGh()
    _issue_at(gh, 5)
    cli = ScriptedCli(
        start=[(1, "", "refused"), (0, "r-2\n", "")],
        status=[(0, _status(_run("r-2", 5)), "")],
    )
    ex = _executor(ccfg, gh, cli)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert [a.status for a in ex.agents()] == ["working"]


def test_stop_of_an_unknown_task_does_nothing(ccfg):
    cli = ScriptedCli()
    ex = _executor(ccfg, FakeGh(), cli)
    ex.apply([StopAgent(task=3)])
    assert cli.calls == []
    assert list(ex.failures) == []


def test_apply_refuses_ledger_effects(ccfg):
    """Ledger effects stay in HerdrExecutor; one reaching here is a routing bug."""
    ex = _executor(ccfg, FakeGh(), ScriptedCli())
    with pytest.raises(ValueError):
        ex.apply([Comment(task=5, body="x")])


# --- agents ---------------------------------------------------------------------


def test_running_run_maps_to_working(ccfg):
    run = _run("r-g", 5, progress=3, output_bytes=1234, run_spent_usd=1.25)
    cli = ScriptedCli(start=[(0, "r-g\n", "")], status=[(0, _status(run), "")])
    ex = _started(ccfg, cli)
    assert ex.agents() == [
        AgentState(
            task=5,
            status="working",
            change_seq=3,
            cost_usd=1.25,
            model=None,
            transcript_bytes=1234,
            paused=False,
        )
    ]
    assert cli.calls_to("status")[-1] == [COMMAND, "status", "--json"]


@pytest.mark.parametrize("status", ["queued", "verifying"])
def test_other_live_statuses_map_to_working(ccfg, status):
    cli = ScriptedCli(start=[(0, "r-g\n", "")], status=[(0, _status(_run("r-g", 5, status)), "")])
    [agent] = _started(ccfg, cli).agents()
    assert (agent.status, agent.paused) == ("working", False)


def test_paused_maps_to_paused_working(ccfg):
    # A null paused_until: no end announced, so no clock is consulted.
    run = _run("r-f", 5, status="paused", paused_until=None, progress=0)
    cli = ScriptedCli(start=[(0, "r-f\n", "")], status=[(0, _status(run), "")])
    [agent] = _started(ccfg, cli).agents()
    assert agent.status == "working"
    assert agent.paused is True
    assert agent.change_seq == 0


def _paused_at(ccfg, paused_until, now):
    run = _run("r-f", 5, status="paused", paused_until=paused_until)
    cli = ScriptedCli(start=[(0, "r-f\n", "")], status=[(0, _status(run), "")])
    gh = FakeGh()
    _issue_at(gh, 5)
    ex = _executor(ccfg, gh, cli, clock=lambda: now)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    [agent] = ex.agents()
    return agent


def test_a_pause_within_its_window_is_paused(ccfg):
    # 600 s of grace past paused_until: still paused one second before it ends.
    assert _paused_at(ccfg, 5000.0, 5599.0).paused is True
    assert _paused_at(ccfg, None, 1e12).paused is True


def test_a_pause_long_past_its_end_is_not_paused(ccfg):
    """A run stuck `paused` forever would otherwise hold its slot all night:
    once paused_until + 600 s has passed, the stall timer runs again."""
    agent = _paused_at(ccfg, 5000.0, 5600.0)
    assert (agent.status, agent.paused) == ("working", False)


def test_waiting_failure_is_recorded_once_per_run(ccfg):
    cli = ScriptedCli(
        start=[(0, "r-g\n", "")], status=[(0, _status(_run("r-g", 5, "waiting")), "")]
    )
    ex = _started(ccfg, cli)
    for _ in range(5):
        ex.agents()
    assert len(ex.failures) == 1


def test_waiting_maps_to_dirty_and_records_a_failure(ccfg):
    """A fleet-mode run never asks the owner; seeing `waiting` is a chain bug."""
    cli = ScriptedCli(
        start=[(0, "r-g\n", "")], status=[(0, _status(_run("r-g", 5, "waiting")), "")]
    )
    ex = _started(ccfg, cli)
    [agent] = ex.agents()
    assert agent.status == "dirty"
    assert len(ex.failures) == 1
    assert "waiting" in ex.failures[0]


def test_task_map_survives_restart(ccfg):
    state_path = ccfg.root / "state" / "chain-runs.json"
    cli = ScriptedCli(start=[(0, "r-g\n", "")], status=[(0, _status(_run("r-g", 5)), "")])
    _started(ccfg, cli, state_path=state_path)
    # Atomic write: no temp file left beside it.
    assert [p.name for p in state_path.parent.iterdir()] == [state_path.name]
    reborn = _executor(ccfg, FakeGh(), cli, state_path=state_path)
    assert reborn.tasks() == {5}
    assert [a.task for a in reborn.agents()] == [5]


@pytest.mark.parametrize(
    "content",
    ["", "{", "{}", "[]", '{"runs": []}', '{"runs": {"x": "r-g"}}', '{"runs": {"5": 7}}'],
    ids=["empty", "truncated", "no-runs", "list", "runs-list", "bad-key", "bad-id"],
)
def test_corrupt_state_file_raises_naming_the_path(ccfg, content):
    """Loud, not empty: an empty map would orphan runs that keep spending."""
    state_path = ccfg.root / "state" / "chain-runs.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(content)
    with pytest.raises(ChainStateError, match=re.escape(str(state_path))):
        _executor(ccfg, FakeGh(), ScriptedCli(), state_path=state_path)


def test_save_fsyncs_before_rename(ccfg, monkeypatch):
    """The rename is atomic only over data that reached the disk."""
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        chain_executor.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        chain_executor.os,
        "replace",
        lambda a, b: (order.append("replace"), real_replace(a, b))[1],
    )
    _started(ccfg, ScriptedCli(start=[(0, "r-g\n", "")]))
    assert order == ["fsync", "replace"]


def test_stop_is_durable_across_restart(ccfg):
    state_path = ccfg.root / "state" / "chain-runs.json"
    cli = ScriptedCli(
        start=[(0, "r-g\n", "")],
        status=[(0, _status(_run("r-g", 5)), "")],
        abort=[(0, "ok\n", "")],
    )
    ex = _started(ccfg, cli, state_path=state_path)
    ex.apply([StopAgent(task=5)])
    assert _executor(ccfg, FakeGh(), cli, state_path=state_path).tasks() == set()


def test_status_failure_keeps_last_known_agents(ccfg):
    good = _status(_run("r-g", 5, progress=4))
    cli = ScriptedCli(
        start=[(0, "r-g\n", "")],
        status=[(0, good, ""), (1, "", "chain: store locked"), (0, "{not json", "")],
    )
    ex = _started(ccfg, cli)
    last = ex.agents()
    assert [a.task for a in last] == [5]
    assert ex.agents() == last  # exit 1
    assert ex.agents() == last  # bad JSON
    assert len(ex.failures) == 2
    assert len(cli.calls_to("status")) == 3


def test_status_failure_before_any_good_read_keeps_mapped_tasks_working(ccfg):
    """After a restart the task map is loaded but no status has been read. A
    chain outage then must not read as every chain agent gone: decide would
    release each as ledger drift and the StopAgent would abort every live run."""
    state_path = ccfg.root / "state" / "chain-runs.json"
    cli = ScriptedCli(start=[(0, "r-g\n", "")], status=[(1, "", "boom")])
    _started(ccfg, cli, state_path=state_path)
    reborn = _executor(ccfg, FakeGh(), cli, state_path=state_path)
    agents = reborn.agents()
    assert agents == [
        AgentState(task=5, status="working", change_seq=-1, cost_usd=0.0, transcript_bytes=0)
    ]
    assert len(reborn.failures) == 1
    task = Task(
        number=5,
        title="t",
        state="claimed",
        area="eval",
        klass="build",
        auto_ok=True,
        attempts=1,
    )
    intents, _ = decide(FleetState(), [task], CHAIN_POLICY, agents, T0)
    assert [i for i in intents if isinstance(i, Release)] == []
    assert cli.calls_to("abort") == []


def test_a_good_read_after_an_outage_replaces_the_placeholder(ccfg):
    """Once a status read succeeds, a mapped run it does not list is absent
    again: the placeholder covers only the gap before the first good read."""
    cli = ScriptedCli(start=[(0, "r-g\n", "")], status=[(1, "", "boom"), (0, _status(), "")])
    ex = _executor(ccfg, FakeGh(), cli)
    ex._runs[5] = "r-g"  # as loaded from a state file
    assert [a.change_seq for a in ex.agents()] == [-1]
    assert ex.agents() == []
    cli.script["status"] = [(1, "", "boom")]
    assert ex.agents() == []  # the last good read, not the placeholder


def test_other_repos_runs_ignored(ccfg):
    """The fixture's runs are all `acme/widgets`; the mapped run id matches one
    of them, so only the slug check keeps it out."""
    fixture_text = FIXTURE.read_text()
    cli = ScriptedCli(start=[(0, "r-g\n", "")], status=[(0, fixture_text, "")])
    ex = _started(ccfg, cli, task=7)
    assert ex.agents() == []
    # Control: the same run under this repo's slug is reported.
    doc = _fixture()
    doc["runs"][0]["fleet_task"]["slug"] = "o/r"
    cli.script["status"] = [(0, json.dumps(doc), "")]
    assert [a.task for a in ex.agents()] == [7]


def test_unmapped_runs_of_this_repo_are_ignored(ccfg):
    """A run fleet did not start (an owner's own `chain start --fleet`) is not
    fleet's agent."""
    cli = ScriptedCli(status=[(0, _status(_run("r-x", 5)), "")])
    assert _executor(ccfg, FakeGh(), cli).agents() == []


def test_unpriced_is_always_empty(ccfg):
    assert _executor(ccfg, FakeGh(), ScriptedCli()).unpriced() == set()


def test_failures_deque_can_be_shared(ccfg):
    shared: deque[str] = deque(maxlen=64)
    ex = _executor(ccfg, FakeGh(), ScriptedCli(status=[(1, "", "x")]), failures=shared)
    ex.agents()
    assert ex.failures is shared
    assert len(shared) == 1


def test_default_failures_are_bounded(ccfg):
    ex = _executor(ccfg, FakeGh(), ScriptedCli(status=[(1, "", "x")]))
    for _ in range(100):
        ex.agents()
    assert len(ex.failures) == 64


# --- outcomes -------------------------------------------------------------------

BASE_SHA = "b" * 40
HEAD_SHA = "c" * 40
INTEGRATION_SHA = "a1" * 20
CLOCK = 1_790_000_000.0
OUTCOME_POLICY = dataclasses.replace(
    CHAIN_POLICY,
    routes=(("build", ("chain", "integrator")),),
)


def _approved(run_id: str, number: int, **kw) -> dict:
    """A run chain ended approved, with valid SHAs (the fixture's `h...` head
    is not hex, and the handoff schema checks)."""
    fields = {
        "stage": "done",
        "outcome": "approved",
        "progress": 7,
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "branch": f"chain/{run_id}",
        "base": "integration",
        "test_command": "make check",
        "pr": {"number": 42, "url": "https://github.com/o/r/pull/42"},
    }
    fields.update(kw)
    return _run(run_id, number, status="approved", **fields)


def _escalated(run_id: str, number: int, **kw) -> dict:
    fields = {
        "stage": "implement",
        "outcome": "escalated",
        "escalation": copy.deepcopy(_fixture()["runs"][3]["escalation"]),
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "branch": f"chain/{run_id}",
    }
    fields.update(kw)
    # A real fleet-mode escalation ends with status `ended`.
    return _run(run_id, number, status="ended", **fields)


def _bug_verdict(run_id: str, number: int, fixture_index: int) -> dict:
    source = _fixture()["runs"][fixture_index]
    return _run(
        run_id,
        number,
        status="ended",
        stage="confirm",
        outcome=source["outcome"],
        verdict=copy.deepcopy(source["verdict"]),
    )


def _finished(ccfg, run: dict, gh=None, state_path=None, **script):
    """An executor whose task 5 was started as `run["id"]` and whose status now
    lists `run`."""
    gh = gh or FakeGh()
    _issue_at(gh, 5)
    cli = ScriptedCli(start=[(0, f"{run['id']}\n", "")], status=[(0, _status(run), "")], **script)
    ex = _executor(ccfg, gh, cli, state_path=state_path, clock=lambda: CLOCK)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    return ex, gh, cli


def _comments(gh: FakeGh, number: int = 5) -> list[str]:
    return gh.get_issue(number)["comments"]


def _handoffs(gh: FakeGh, number: int = 5) -> list[str]:
    return [c for c in _comments(gh, number) if packet.MARKERS["handoff"] in c]


def _verdicts(gh: FakeGh, number: int = 5) -> list[str]:
    return [c for c in _comments(gh, number) if packet.MARKERS["verdict"] in c]


def _state_labels(gh: FakeGh, number: int = 5) -> list[str]:
    return [n for n in gh.get_issue(number)["labels"] if n in labels.all_state_labels()]


def test_approved_run_hands_to_review_then_approves(ccfg):
    ex, gh, _ = _finished(ccfg, _approved("r-e", 5))
    ex.agents()
    assert _state_labels(gh) == ["fleet:integrating"]
    assert len(_handoffs(gh)) == 1
    assert len(_verdicts(gh)) == 1
    handoff = packet.parse_latest(_comments(gh), "handoff")
    verdict = packet.parse_latest(_comments(gh), "verdict")
    validate(ccfg, "handoff", handoff)
    validate(ccfg, "verdict", verdict)
    assert handoff["to"] == "review"
    assert handoff["from"] == {
        "role": "chain",
        "agent": "chain-r-e",
        "run": datetime.fromtimestamp(CLOCK, UTC).isoformat(),
    }
    assert (handoff["base_sha"], handoff["head_sha"]) == (BASE_SHA, HEAD_SHA)
    assert handoff["branch"] == "chain/r-e"
    assert handoff["did"] == "chain run r-e: plan, audit, implement, review"
    assert handoff["evidence"]["checks"] == "test command passed at the reviewed SHA"
    assert handoff["evidence"]["commands"] == ["make check"]
    assert verdict["verdict"] == "approved"
    assert verdict["head_sha"] == HEAD_SHA
    assert verdict["reviewer"] == "chain-r-e"
    assert verdict["findings"] == []
    assert list(ex.failures) == []


def test_escalated_run_moves_to_human_with_question(ccfg):
    ex, gh, _ = _finished(ccfg, _escalated("r-d", 5))
    ex.agents()
    assert _state_labels(gh) == ["fleet:human"]
    found = escalation.latest(_comments(gh))
    assert found["text"].startswith("Which database?")
    assert "chain run r-d ended at stage implement (needs_owner)" in found["text"]
    validate(ccfg, "handoff", packet.parse_latest(_comments(gh), "handoff"))
    assert list(ex.failures) == []


def test_escalated_outcome_unassigns(ccfg):
    ex, gh, _ = _finished(ccfg, _escalated("r-d", 5))
    gh.assign(5, "FibonAdithya")
    ex.agents()
    assert gh.get_issue(5)["assignees"] == []


def test_not_reproduced_closes_with_comment(ccfg):
    ex, gh, _ = _finished(ccfg, _bug_verdict("r-c", 5, 4))
    ex.agents()
    issue = gh.get_issue(5)
    assert issue["state"] == "CLOSED"
    assert _state_labels(gh) == ["fleet:done"]
    [comment] = _comments(gh)
    assert "ran the repro twice" in comment
    assert list(ex.failures) == []


def test_duplicate_closes_naming_the_duplicate(ccfg):
    ex, gh, _ = _finished(ccfg, _bug_verdict("r-b", 5, 5))
    ex.agents()
    assert gh.get_issue(5)["state"] == "CLOSED"
    assert _state_labels(gh) == ["fleet:done"]
    [comment] = _comments(gh)
    assert comment.startswith("Duplicate of #12")
    assert comment.splitlines()[-1] == "<!-- fleet:chain-outcome run=r-b -->"


@pytest.mark.parametrize("status", ["aborted", "ended"])
def test_aborted_run_reports_dirty(ccfg, status):
    """A run that ended with no outcome writes nothing: `_fail` retries it."""
    ex, gh, _ = _finished(ccfg, _run("r-a", 5, status=status, outcome=None, progress=4))
    [agent] = ex.agents()
    assert agent.status == "dirty"
    assert _comments(gh) == []
    assert _state_labels(gh) == ["fleet:claimed"]


def test_escalation_before_setup_uses_integration_head(ccfg):
    run = _escalated("r-d", 5, base_sha=None, head_sha=None, branch=None)
    ex, gh, cli = _finished(ccfg, run, **{"-C": [(0, f"{INTEGRATION_SHA}\n", "")]})
    ex.agents()
    assert cli.calls_to("-C") == [["git", "-C", str(ccfg.root), "rev-parse", "origin/integration"]]
    handoff = packet.parse_latest(_comments(gh), "handoff")
    validate(ccfg, "handoff", handoff)
    assert (handoff["base_sha"], handoff["head_sha"]) == (INTEGRATION_SHA, INTEGRATION_SHA)
    assert _state_labels(gh) == ["fleet:human"]


class RecordingGh(FakeGh):
    """FakeGh that records every write it is asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple] = []

    def comment(self, issue: int, body: str) -> None:
        self.writes.append(("comment", issue))
        super().comment(issue, body)

    def add_labels(self, issue: int, labels: list[str]) -> None:
        self.writes.append(("add_labels", issue, tuple(labels)))
        super().add_labels(issue, labels)

    def remove_labels(self, issue: int, labels: list[str]) -> None:
        self.writes.append(("remove_labels", issue, tuple(labels)))
        super().remove_labels(issue, labels)

    def close_issue(self, issue: int) -> None:
        self.writes.append(("close", issue))
        super().close_issue(issue)

    def assign(self, issue: int, login: str | None) -> None:
        self.writes.append(("assign", issue, login))
        super().assign(issue, login)


@pytest.mark.parametrize(
    "run",
    [_approved("r-e", 5), _escalated("r-d", 5), _bug_verdict("r-c", 5, 4)],
    ids=["approved", "escalated", "not_reproduced"],
)
def test_outcome_written_once_across_restart(ccfg, run):
    """Two agents() calls, then a new executor on the same state file: the
    outcome's writes happen once, and the restarted executor writes nothing
    at all (not even a label move back and forth)."""
    state_path = ccfg.root / "state" / "chain-runs.json"
    ex, gh, cli = _finished(ccfg, run, gh=RecordingGh(), state_path=state_path)
    ex.agents()
    ex.agents()
    after_first = list(gh.writes)
    reborn = _executor(ccfg, gh, cli, state_path=state_path, clock=lambda: CLOCK)
    reborn.agents()
    reborn.agents()
    assert gh.writes == after_first
    # handoff + verdict; one escalation packet; one verdict comment.
    expected = {"approved": 2, "escalated": 1, "not_reproduced": 1}[run["outcome"]]
    assert len(_comments(gh)) == expected
    assert len(_handoffs(gh)) == (0 if run["outcome"] == "not_reproduced" else 1)


class FlakyVerdictGh(FakeGh):
    """FakeGh whose first verdict comment fails, as a gh timeout would: the
    comment is not posted and the labels do not move."""

    def __init__(self) -> None:
        super().__init__()
        self.verdict_failures = 1

    def comment(self, issue: int, body: str) -> None:
        if packet.MARKERS["verdict"] in body and self.verdict_failures:
            self.verdict_failures -= 1
            raise GhError("gh: timed out")
        super().comment(issue, body)


def test_failed_write_is_retried_next_tick(ccfg):
    ex, gh, _ = _finished(ccfg, _approved("r-e", 5), gh=FlakyVerdictGh())
    [agent] = ex.agents()
    assert agent.status == "working"
    assert _state_labels(gh) == ["fleet:review"]
    assert _verdicts(gh) == []
    assert len(ex.failures) == 1
    ex.agents()
    assert len(_handoffs(gh)) == 1
    assert len(_verdicts(gh)) == 1
    assert _state_labels(gh) == ["fleet:integrating"]


def test_failed_status_read_writes_no_outcome(ccfg):
    """Writes come only from a successful read of this call."""
    ex, gh, cli = _finished(ccfg, _approved("r-e", 5))
    cli.script["status"] = [(1, "", "chain: store locked")]
    ex._last_runs = parse_status(_status(_approved("r-e", 5)))
    ex._read_once = True
    ex.agents()
    assert _comments(gh) == []


def _ledger_task(state: str, *, is_open: bool = True) -> Task:
    return Task(
        number=5,
        title="Build it",
        state=state,
        area="eval",
        klass="build",
        auto_ok=True,
        attempts=1,
        is_open=is_open,
    )


def _stop_effects(intents) -> list[StopAgent]:
    return [
        effect
        for intent in intents
        for effect in expand(intent, OUTCOME_POLICY)
        if isinstance(effect, StopAgent)
    ]


def _race(ccfg, run_spent_usd: float = 1.25):
    """Drive decide() across the ticks after chain approves task 5.

    The daemon reads the ledger before it calls agents(), so on the tick the
    outcome is written decide sees the task still at `claimed`, with an
    observation from before the run ended and older than `stall_minutes`."""
    run = _approved("r-e", 5, progress=7, output_bytes=900, run_spent_usd=run_spent_usd)
    ex, gh, cli = _finished(ccfg, run)
    stale = Observation(change_seq=7, since=T0 - timedelta(minutes=30), transcript_bytes=900)
    prev = FleetState(seen=((5, stale),))
    ticks = []
    # Tick 1: the write tick, decided over the ledger read before the writes.
    agents = ex.agents()
    intents, state = decide(prev, [_ledger_task("claimed")], OUTCOME_POLICY, agents, T0)
    ticks.append((agents, intents, state))
    # Tick 2: the ledger now shows the label the outcome wrote.
    agents = ex.agents()
    intents, state = decide(
        state, [_ledger_task("integrating")], OUTCOME_POLICY, agents, T0 + timedelta(minutes=1)
    )
    ticks.append((agents, intents, state))
    ex.apply(_stop_effects(intents))
    # Tick 3: the executor has let the run go.
    agents = ex.agents()
    intents, state = decide(
        state, [_ledger_task("integrating")], OUTCOME_POLICY, agents, T0 + timedelta(minutes=2)
    )
    ticks.append((agents, intents, state))
    return ticks, gh, cli


def test_no_release_race_after_outcome(ccfg):
    """Mutations caught: reporting the run `done` on the write tick (decide's
    done-without-packet path falls to the stall check and releases it), and
    dropping the agent on the write tick (a `no_agent` release). Either would
    relabel the task `ready` over the `integrating` label just written."""
    ticks, gh, cli = _race(ccfg)
    (a1, i1, _), (a2, i2, _), (a3, i3, _) = ticks
    assert [a.status for a in a1] == ["working"]
    assert [i for i in i1 if i.task == 5 and isinstance(i, (Release, Escalate))] == []
    assert [a.status for a in a2] == ["done"]
    assert Retire(task=5) in i2
    assert [i for i in i2 if i.task == 5 and isinstance(i, (Release, Escalate))] == []
    assert a3 == []
    assert [i for i in i3 if isinstance(i, Continue)] == [
        Continue(task=5, role="integrator", base="integration")
    ]
    # The run had ended: letting it go aborts nothing.
    assert cli.calls_to("abort") == []
    assert _state_labels(gh) == ["fleet:integrating"]


def test_chain_cost_is_charged_once(ccfg):
    """A chain agent dropped without being reaped is never charged; one reaped
    twice would be charged twice."""
    ticks, _, _ = _race(ccfg, run_spent_usd=12.5)
    assert [state.spent_usd for _, _, state in ticks] == [0.0, 12.5, 12.5]


def test_outcome_stop_forgets_without_abort_after_restart(ccfg):
    """After a restart with no good read yet, the run's status is unknown; a
    run whose outcome was written is still not aborted."""
    state_path = ccfg.root / "state" / "chain-runs.json"
    ex, gh, cli = _finished(ccfg, _approved("r-e", 5), state_path=state_path)
    ex.agents()
    cli.script["status"] = [(1, "", "chain: store locked")]
    reborn = _executor(ccfg, gh, cli, state_path=state_path, clock=lambda: CLOCK)
    [agent] = reborn.agents()
    assert agent.status == "done"
    reborn.apply([StopAgent(task=5)])
    assert cli.calls_to("abort") == []
    assert reborn.tasks() == set()


@pytest.mark.parametrize(
    "outcomes",
    [
        [],
        {"r-e": []},
        {"r-e": {"outcome": "approved", "steps": "handoff"}},
        {"r-e": {"outcome": "approved", "steps": [1]}},
        {"r-e": {"outcome": "approved", "steps": [], "cost_usd": "1"}},
        {"r-e": {"outcome": 3, "steps": [], "cost_usd": 1.0, "progress": 1, "output_bytes": 0}},
        {
            "r-e": {
                "outcome": "approved",
                "steps": [],
                "cost_usd": 1.0,
                "progress": 1,
                "output_bytes": 0,
                "fallback": 7,
            }
        },
    ],
    ids=["list", "record-list", "steps-str", "step-int", "cost-str", "outcome-int", "fallback-int"],
)
def test_corrupt_outcome_records_raise_naming_the_path(ccfg, outcomes):
    state_path = ccfg.root / "state" / "chain-runs.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"runs": {"5": "r-e"}, "outcomes": outcomes}))
    with pytest.raises(ChainStateError, match=re.escape(str(state_path))):
        _executor(ccfg, FakeGh(), ScriptedCli(), state_path=state_path)


class FlakyLabelGh(FakeGh):
    """FakeGh whose first move to `fleet:review` fails after the handoff
    comment has landed: `_post` comments, then moves the label."""

    def __init__(self) -> None:
        super().__init__()
        self.label_failures = 1

    def add_labels(self, issue: int, labels: list[str]) -> None:
        if "fleet:review" in labels and self.label_failures:
            self.label_failures -= 1
            raise GhError("gh: timed out")
        super().add_labels(issue, labels)


def test_handoff_posted_but_not_labelled_is_not_posted_again(ccfg):
    """The retry finds its own packet on the issue and only moves the label.
    Also covers a crash between a write and the state-file save."""
    ex, gh, _ = _finished(ccfg, _approved("r-e", 5), gh=FlakyLabelGh())
    ex.agents()
    assert len(_handoffs(gh)) == 1
    assert _state_labels(gh) == ["fleet:claimed"]
    ex.agents()
    assert len(_handoffs(gh)) == 1
    assert len(_verdicts(gh)) == 1
    assert _state_labels(gh) == ["fleet:integrating"]


def test_a_crash_before_saving_a_step_does_not_repost(ccfg):
    """The outcome landed but the state file never heard: a restart finds
    the packets on the issue instead of posting them again."""
    state_path = ccfg.root / "state" / "chain-runs.json"
    ex, gh, cli = _finished(ccfg, _approved("r-e", 5), state_path=state_path)
    saved = state_path.read_text()
    ex.agents()
    state_path.write_text(saved)  # as if every save after the start was lost
    reborn = _executor(ccfg, gh, cli, state_path=state_path, clock=lambda: CLOCK)
    reborn.agents()
    assert len(_handoffs(gh)) == 1
    assert len(_verdicts(gh)) == 1
    assert _state_labels(gh) == ["fleet:integrating"]


def test_escalation_posted_but_not_labelled_is_not_posted_again(ccfg):
    class FlakyHumanGh(FakeGh):
        failures = 1

        def add_labels(self, issue, labels):
            if "fleet:human" in labels and self.failures:
                self.failures -= 1
                raise GhError("gh: timed out")
            super().add_labels(issue, labels)

    ex, gh, _ = _finished(ccfg, _escalated("r-d", 5), gh=FlakyHumanGh())
    ex.agents()
    ex.agents()
    assert len(_handoffs(gh)) == 1
    assert _state_labels(gh) == ["fleet:human"]


def test_fixture_outcomes_drive_the_writes(ccfg):
    """chain's own contract fixture, read through parse_status, drives the
    writes. Catches escalation keyed on status `escalated` (a fleet-mode
    escalation ends `ended`) and `tried` read as one string (it is a list)."""
    doc = _fixture()
    by_id = {run["id"]: run for run in doc["runs"]}
    tasks = {"r-d": 5, "r-c": 6, "r-e": 7}
    for run in doc["runs"]:
        # This repository's slug, rewritten here, never in the fixture file.
        run["fleet_task"]["slug"] = "o/r"
    for run_id, task in tasks.items():
        by_id[run_id]["fleet_task"]["number"] = task
    # The fixture's approved head (`h` * 40) is not hex; the handoff schema
    # checks, and a real run reports a real SHA.
    by_id["r-e"]["head_sha"] = HEAD_SHA
    runs = {run.id: run for run in parse_status(json.dumps(doc))}
    assert (runs["r-d"].status, runs["r-d"].outcome) == ("ended", "escalated")
    assert runs["r-c"].verdict["tried"] == ["ran the repro twice"]

    gh = FakeGh()
    _issue_at(gh, 5, klass="build")
    _issue_at(gh, 6, klass="bug", body="It crashes.\n")
    _issue_at(gh, 7, klass="build")
    cli = ScriptedCli(
        start=[(0, "r-d\n", ""), (0, "r-c\n", ""), (0, "r-e\n", "")],
        status=[(0, json.dumps(doc), "")],
        # r-d escalated before setup: no SHAs of its own.
        **{"-C": [(0, f"{INTEGRATION_SHA}\n", "")]},
    )
    ex = _executor(ccfg, gh, cli, clock=lambda: CLOCK)
    for task in tasks.values():
        ex.apply([StartAgent(task=task, role="chain", base="integration")])
    ex.agents()
    assert list(ex.failures) == []

    # r-d: status ended, outcome escalated -> the owner's question.
    assert _state_labels(gh, 5) == ["fleet:human"]
    found = escalation.latest(_comments(gh, 5))
    assert found["text"].startswith(runs["r-d"].escalation["text"])
    # r-c: not reproduced, each tried string a bullet of the closing comment.
    assert gh.get_issue(6)["state"] == "CLOSED"
    [comment] = _comments(gh, 6)
    for tried in runs["r-c"].verdict["tried"]:
        assert f"- {tried}" in comment.splitlines()
    # r-e: approved -> handed to review and approved.
    assert _state_labels(gh, 7) == ["fleet:integrating"]
    assert len(_handoffs(gh, 7)) == 1
    assert packet.parse_latest(_comments(gh, 7), "verdict")["verdict"] == "approved"


@pytest.mark.parametrize(
    ("tried", "expected"),
    [
        (
            ["ran the repro twice", "bisected to v1.2"],
            ["- ran the repro twice", "- bisected to v1.2"],
        ),
        ("ran the repro twice", ["- ran the repro twice"]),
        (["", "  ", 3, "kept"], ["- kept"]),
    ],
    ids=["list", "older-string", "blanks-and-non-strings"],
)
def test_not_reproduced_lists_what_was_tried(ccfg, tried, expected):
    run = _bug_verdict("r-c", 5, 4)
    run["verdict"]["tried"] = tried
    ex, gh, _ = _finished(ccfg, run)
    ex.agents()
    [comment] = _comments(gh)
    assert [line for line in comment.splitlines() if line.startswith("- ")] == expected


@pytest.mark.parametrize("tried", [None, [], "", {"cmd": "x"}, 7], ids=repr)
def test_not_reproduced_without_tried_says_so(ccfg, tried):
    run = _bug_verdict("r-c", 5, 4)
    run["verdict"]["tried"] = tried
    ex, gh, _ = _finished(ccfg, run)
    ex.agents()
    [comment] = _comments(gh)
    assert "chain did not say what it tried." in comment
    assert gh.get_issue(5)["state"] == "CLOSED"


# --- held cost, permanent failures ----------------------------------------------


def _intents_for(ticks, task: int = 5, kinds=(Release, Escalate)):
    # Not every intent has a task: a fleet over its budget also drains.
    return [
        i
        for _, intents, _ in ticks
        for i in intents
        if isinstance(i, kinds) and getattr(i, "task", None) == task
    ]


def test_no_race_when_run_cost_exceeds_chain_ceiling(ccfg):
    """A run that finished above the chain ceiling (40) is not released as
    over budget on the write tick over the label it just wrote: the held
    report carries no cost, and the `done` report that Retire reaps does."""
    ticks, gh, _ = _race(ccfg, run_spent_usd=50.0)
    assert _intents_for(ticks) == []
    assert Retire(task=5) in ticks[1][1]
    assert ticks[-1][2].spent_usd == 50.0
    assert _state_labels(gh) == ["fleet:integrating"]


def test_flaky_verdict_does_not_escalate_over_budget(ccfg):
    """The verdict fails once, so on the next tick the ledger reads `review`
    while the run is still held. Its cost (12.5) against the integrator's
    ceiling (2.0) would escalate it as over budget if the held report
    carried it."""
    run = _approved("r-e", 5, progress=7, output_bytes=900, run_spent_usd=12.5)
    ex, gh, _ = _finished(ccfg, run, gh=FlakyVerdictGh())
    stale = Observation(change_seq=7, since=T0 - timedelta(minutes=30), transcript_bytes=900)
    state = FleetState(seen=((5, stale),))
    ticks = []
    for minute, ledger_state in enumerate(["claimed", "review", "integrating"]):
        agents = ex.agents()
        intents, state = decide(
            state,
            [_ledger_task(ledger_state)],
            OUTCOME_POLICY,
            agents,
            T0 + timedelta(minutes=minute),
        )
        ticks.append((agents, intents, state))
        ex.apply(_stop_effects(intents))
    assert _intents_for(ticks) == []
    assert Retire(task=5) in ticks[2][1]
    assert state.spent_usd == 12.5
    assert _state_labels(gh) == ["fleet:integrating"]
    assert len(_handoffs(gh)) == 1
    assert len(_verdicts(gh)) == 1


@pytest.mark.parametrize(
    ("run", "needs", "failures"),
    [
        (
            _escalated("r-d", 5, escalation={"kind": "needs_owner", "text": "  ", "options": []}),
            "chain run r-d escalated at stage implement without a question",
            0,
        ),
        (
            dict(
                _bug_verdict("r-b", 5, 5),
                verdict={"issue_verdict": "duplicate", "duplicate_of": None, "tried": None},
            ),
            "chain run r-b ended with outcome duplicate, but fleet could not record it",
            1,
        ),
        (
            _approved("r-e", 5, head_sha="h" * 40),
            "chain run r-e ended with outcome approved, but fleet could not record it",
            1,
        ),
        (
            _approved("r-e", 5, test_command=None),
            "chain run r-e ended with outcome approved, but fleet could not record it",
            1,
        ),
    ],
    ids=[
        "blank-escalation-text",
        "null-duplicate-of",
        "non-hex-head-sha",
        "approved-null-test-command",
    ],
)
def test_unrecordable_outcome_escalates_once(ccfg, run, needs, failures):
    """A step that can never succeed must not loop (stall, release, a new
    chain run, the result thrown away): fleet escalates naming the run, once,
    and records the failure once."""
    ex, gh, cli = _finished(ccfg, run, **{"-C": [(0, f"{INTEGRATION_SHA}\n", "")]})
    for _ in range(3):
        ex.agents()
    assert _state_labels(gh) == ["fleet:human"]
    found = escalation.latest(_comments(gh))
    assert found["text"].startswith(needs)
    escalations = [c for c in _handoffs(gh) if packet.parse_latest([c], "handoff")["to"] == "human"]
    assert len(escalations) == 1
    assert len(_handoffs(gh)) == 1
    validate(ccfg, "handoff", packet.parse_latest(_comments(gh), "handoff"))
    assert gh.get_issue(5)["assignees"] == []
    assert len(ex.failures) == failures
    # The run is held, then done -- never dirty, so `_fail` never retries it.
    assert ex.agents()[0].status == "done"


def test_permanent_failure_after_a_landed_step_keeps_it(ccfg):
    """Steps that landed stay landed: the handoff is not withdrawn when the
    verdict cannot be written, and the fallback escalation follows it."""

    class BadVerdictGh(FakeGh):
        def comment(self, issue, body):
            if packet.MARKERS["verdict"] in body:
                raise ValueError("verdict refused")
            super().comment(issue, body)

    ex, gh, _ = _finished(ccfg, _approved("r-e", 5), gh=BadVerdictGh())
    ex.agents()
    ex.agents()
    handoffs = [packet.parse_latest([c], "handoff")["to"] for c in _handoffs(gh)]
    assert handoffs == ["review", "human"]
    assert _state_labels(gh) == ["fleet:human"]
    assert "verdict refused" in escalation.latest(_comments(gh))["text"]


def test_closing_comment_not_reposted_after_crash_before_save(ccfg):
    state_path = ccfg.root / "state" / "chain-runs.json"
    ex, gh, cli = _finished(ccfg, _bug_verdict("r-c", 5, 4), state_path=state_path)
    saved = state_path.read_text()
    ex.agents()
    state_path.write_text(saved)  # every save after the start lost
    reborn = _executor(ccfg, gh, cli, state_path=state_path, clock=lambda: CLOCK)
    reborn.agents()
    assert len(_comments(gh)) == 1
    assert _comments(gh)[0].splitlines()[-1] == "<!-- fleet:chain-outcome run=r-c -->"
    assert gh.get_issue(5)["state"] == "CLOSED"
