from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fleet import chain_executor
from fleet.chain_executor import ChainExecutor, ChainRun, ChainStateError, parse_status
from fleet.decide import decide
from fleet.gh import FakeGh
from fleet.model import (
    AgentState,
    Comment,
    FleetState,
    Policy,
    Release,
    StartAgent,
    StopAgent,
    Task,
)

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
        "escalated",
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
    cli = ScriptedCli(start=[(0, "r-1\n", "")])
    ex = _executor(ccfg, gh, cli)
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert "--answers" not in cli.calls_to("start")[0]
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


def test_start_over_a_live_mapped_run_aborts_it_first(ccfg):
    cli = ScriptedCli(
        start=[(0, "r-1\n", ""), (0, "r-2\n", "")],
        status=[(0, _status(_run("r-1", 5)), "")],
        abort=[(1, "", "chain: busy")],
    )
    ex = _started(ccfg, cli)
    ex.agents()
    ex.apply([StartAgent(task=5, role="chain", base="integration")])
    assert cli.calls_to("abort") == [[COMMAND, "abort", "r-1"]]
    assert len(ex.failures) == 1  # the failed abort
    assert ex._runs == {5: "r-2"}
    # Ordering: the abort precedes the second start.
    assert [argv[1] for argv in cli.calls] == ["start", "status", "abort", "start"]


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
