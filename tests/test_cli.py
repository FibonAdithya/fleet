from __future__ import annotations

import dataclasses
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from conftest import FIXTURE_POLICY, git
from fleet import chain_executor
from fleet.cli import ReadOnlyGh, _context, _git_facts, main
from fleet.composite_executor import CompositeExecutor
from fleet.config import load_config
from fleet.daemon import Daemon
from fleet.gh import FakeGh
from fleet.herdr import FakeHerdr
from fleet.model import FleetSnapshot, Release
from fleet.runlog import RunLog
from fleet.schemas import validate


@pytest.fixture()
def env(cfg, monkeypatch):
    monkeypatch.chdir(cfg.root)
    monkeypatch.delenv("FLEET_AGENT", raising=False)
    monkeypatch.delenv("FLEET_ROLE", raising=False)
    return monkeypatch


def test_an_exported_but_empty_fleet_agent_is_treated_as_unset(env, capsys):
    """`export FLEET_AGENT="$AGENT_ID"` with AGENT_ID unset exports "", which is
    not None -- so the warning and the fallback were both skipped and the packet
    failed validation with "from.agent: '' is too short", a complaint about a
    field the operator never set."""
    env.setenv("FLEET_AGENT", "")
    env.setenv("FLEET_ROLE", "implementer")
    _, _, identity = _context()
    assert identity["agent"] == "manual"
    assert "FLEET_AGENT unset" in capsys.readouterr().err


def test_an_unset_fleet_role_is_an_error_not_an_implementer(env):
    """Defaulting to "implementer" hands out exactly the privilege the role gate
    withholds: an unconfigured run could then hand its own task to `done`, or
    fail `--to done` for a real integrator with a message about a role it never
    chose. This is the only place that can still tell unset from chosen, so it is
    the only place that can refuse."""
    env.setenv("FLEET_AGENT", "fleet-3")
    with pytest.raises(click.ClickException, match="FLEET_ROLE"):
        _context()


def test_an_exported_but_empty_fleet_role_is_an_error_too(env):
    """`export FLEET_ROLE="$ROLE"` with ROLE unset exports "", not None -- the
    same shape as the FLEET_AGENT case above, and it must not slip past the
    check."""
    env.setenv("FLEET_AGENT", "fleet-3")
    env.setenv("FLEET_ROLE", "")
    with pytest.raises(click.ClickException, match="FLEET_ROLE"):
        _context()


def test_discover_needs_no_role_at_all(env):
    """`fleet discover` records no role and gates no transition, so requiring one
    would refuse a human triaging by hand and buy nothing. Catches making the
    refusal unconditional."""
    env.setenv("FLEET_AGENT", "fleet-3")
    _, _, identity = _context(role_required=False)
    assert identity["role"] == ""


def test_a_real_agent_id_is_used_and_warns_about_nothing(env, capsys):
    env.setenv("FLEET_AGENT", "fleet-3")
    env.setenv("FLEET_ROLE", "implementer")
    _, _, identity = _context()
    assert identity["agent"] == "fleet-3"
    assert capsys.readouterr().err == ""


def test_the_generated_run_timestamp_satisfies_the_schema(cfg, env):
    """The CLI formats `run` itself, and the schema's date-time format is now
    enforced -- so a change to that strftime has to fail here."""
    env.setenv("FLEET_AGENT", "fleet-3")
    env.setenv("FLEET_ROLE", "implementer")
    _, _, identity = _context()
    validate(
        cfg,
        "handoff",
        {
            "schema": "fleet.handoff.v1",
            "task": 1,
            "to": "review",
            "attempt": 1,
            "from": identity,
            "base_sha": "fb5ec5d",
            "head_sha": "a91c204",
            "branch": "b",
            "did": "d",
            "evidence": {"checks": "c", "commands": ["make check"]},
        },
    )


def test_git_facts_refuses_a_detached_head_for_a_handoff(repo):
    cfg = SimpleNamespace(root=repo, integration_branch="integration")
    git(repo, "checkout", "--detach", "HEAD")
    with pytest.raises(RuntimeError, match="detached HEAD"):
        _git_facts(cfg)


def test_git_facts_allows_a_detached_head_for_escalate_and_block(repo):
    cfg = SimpleNamespace(root=repo, integration_branch="integration")
    git(repo, "checkout", "--detach", "HEAD")
    assert _git_facts(cfg, allow_detached=True)["branch"].startswith("detached at ")


# -- `fleet plan` is read-only, with no flag to make it otherwise ------------


def test_plan_always_wraps_the_client_in_read_only(monkeypatch, cfg):
    """No longer a default -- the only behaviour. `--no-dry-run` selected an
    unwrapped writing client for a body that makes no write, so it was a flag
    that did nothing; the test that claimed to pin it monkeypatched `do_plan`
    away and could not have noticed. It is gone, and "writes nothing" is now a
    property of the code path with no way to opt out of it.

    tests/test_plan.py holds the other half: that the option is refused and
    that the help says so."""
    monkeypatch.chdir(cfg.root)
    seen = {}
    monkeypatch.setattr(
        "fleet.cli.do_plan", lambda cfg_, gh, now, **kw: seen.setdefault("gh", gh) and []
    )
    monkeypatch.setattr("fleet.cli._gh_login", lambda: "FibonAdithya")
    result = CliRunner().invoke(main, ["plan"])
    assert result.exit_code == 0, result.output
    assert isinstance(seen["gh"], ReadOnlyGh)


# -- `fleet status` ----------------------------------------------------------


def test_status_with_no_daemon_running_exits_nonzero_and_says_so(monkeypatch, cfg):
    """At 7am the one thing `status` has to answer is whether the fleet is dead
    or merely idle. Nothing is listening on `cfg.socket_path`, so this must
    fail loudly rather than render an empty ledger."""
    monkeypatch.chdir(cfg.root)
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code != 0
    assert "no fleet daemon" in result.output.lower()


# -- `fleet run --once` ------------------------------------------------------


def test_run_once_builds_its_collaborators_and_ticks_exactly_once(monkeypatch, tmp_path):
    """Wired, not exercised end to end: `Daemon.tick` is monkeypatched so this
    never talks to `gh` or a real herdr, but `run` must still load config,
    build the policy, the real gh/herdr/executor, the run log and the daemon,
    then call `tick` exactly once and print the rendered snapshot."""
    policy_cfg = load_config(FIXTURE_POLICY)
    cfg = dataclasses.replace(
        policy_cfg,
        socket_path=tmp_path / "fleet.sock",
        runlog_path=tmp_path / "run.jsonl",
        worktree_root=tmp_path / "worktrees",
        answers_dir=tmp_path / "answers",
    )

    calls = []

    def fake_tick(self, now):
        calls.append(now)
        self.snapshot = FleetSnapshot(
            run_id=self.run_id,
            tick=self.tick_number + 1,
            at=now.isoformat(),
            rows=(),
            spent_usd=0.0,
            fleet_usd=self.policy.fleet_usd,
            free_slots=self.policy.slots,
            held_areas=(),
        )
        return self.snapshot

    monkeypatch.setattr(Daemon, "tick", fake_tick)
    monkeypatch.setattr("fleet.cli.load_config", lambda: cfg)
    monkeypatch.setattr("fleet.cli._gh_login", lambda: "fleet-bot")

    result = CliRunner().invoke(main, ["run", "--once"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "tick 1" in result.output
    # The socket thread must have been started and cleanly torn down: no
    # leftover socket file after `run --once` returns.
    assert not cfg.socket_path.exists()


# -- `fleet run` and the run id ---------------------------------------------


def _run_cfg(tmp_path):
    """The vendored fixture policy, with every path pointed at a tmp dir."""
    return dataclasses.replace(
        load_config(FIXTURE_POLICY),
        socket_path=tmp_path / "fleet.sock",
        runlog_path=tmp_path / "run.jsonl",
        worktree_root=tmp_path / "worktrees",
        answers_dir=tmp_path / "answers",
    )


def _fake_backends(monkeypatch, cfg, gh, herdr):
    """Wire `fleet run` to the in-memory clients, at the seams the CLI builds.

    Everything else on the path -- config, policy, the run id, the run log, the
    Daemon, the socket, the tick itself -- is the real thing. That is the whole
    point: the restart defect lived in the wiring `Daemon`'s own tests never
    execute.
    """
    monkeypatch.setattr("fleet.cli.load_config", lambda: cfg)
    monkeypatch.setattr("fleet.cli.RealGh", lambda slug: gh)
    monkeypatch.setattr("fleet.cli.RealHerdr", lambda **kwargs: herdr)
    monkeypatch.setattr("fleet.cli._gh_login", lambda: "FibonAdithya")
    # The fixture policy routes build and bug to chain, so `fleet run` builds a
    # ChainExecutor. Answered as a `chain` that is not installed, so no test
    # runs a real one found on PATH.
    monkeypatch.setattr(
        "fleet.chain_executor.run_cli", lambda argv: (127, "", f"{argv[0]}: not installed")
    )


def _run_id_of(output: str) -> str:
    line = next(line for line in output.splitlines() if line.startswith("fleet run:"))
    return line.split("run_id=", 1)[1].split()[0]


def test_a_restarted_fleet_run_resumes_the_night_s_spend(monkeypatch, tmp_path):
    """Spec §8, taken by the only path a human ever starts a daemon on.

    `fleet run` minted `run_id` from the wall clock at every start while
    `Daemon` recovers spend with `spent_from_log(path, run_id=run_id)` -- so a
    restarted daemon filtered on an id that appears in zero log lines and
    resumed at `spent_usd = 0.0`. Spec §8's "spent_usd after the restart equals
    what it was before" was true of `Daemon` and false of `fleet run`, and the
    restart test passed only because it handed both daemons `run_id="tonight"`
    by hand.

    Two real ticks through the CLI, with only gh and herdr faked. The first
    spends $7.50 reaping a dirty agent; the second must start knowing it.
    """
    cfg = _run_cfg(tmp_path)
    gh, herdr = FakeGh(), FakeHerdr(worktree_root=cfg.worktree_root)
    _fake_backends(monkeypatch, cfg, gh, herdr)
    number = gh.create_issue(
        "Fix LID",
        "b",
        ["fleet:claimed", "fleet:attempt-1", "area:eval", "class:patch", "fleet:auto-ok"],
    )
    herdr.add_agent(task=number, status="dirty", change_seq=2, cost_usd=7.5)

    first = CliRunner().invoke(main, ["run", "--once"])
    assert first.exit_code == 0, first.output
    assert "new run" in first.output

    second = CliRunner().invoke(main, ["run", "--once"])
    assert second.exit_code == 0, second.output
    assert _run_id_of(second.output) == _run_id_of(first.output)
    assert "resumed from the run log" in second.output
    # The line an operator reads at 2am: this process knows what the last one
    # spent. `$0.00` here is the defect, and it is silent without this.
    assert "spent=$7.50" in second.output


def test_fleet_run_adopts_the_owners_agent_reported_issue(monkeypatch, tmp_path):
    """The daemon has to know whose issues are the owner's: `fleet run` passes
    `gh`'s login to the Daemon, which passes it to the ledger read. Without that
    wiring no row reads `by_owner` and ADOPT never fires outside its unit tests.
    A row by anyone else is left in triage."""
    cfg = dataclasses.replace(_run_cfg(tmp_path), chain_default_area="eval")
    gh, herdr = FakeGh(), FakeHerdr(worktree_root=cfg.worktree_root)
    _fake_backends(monkeypatch, cfg, gh, herdr)
    gh.ensure_label("agent-reported")
    mine = gh.create_issue("lid rounds early", "b", ["agent-reported"])
    theirs = gh.create_issue("lid rounds late", "b", ["agent-reported"], author="someone-else")

    result = CliRunner().invoke(main, ["run", "--once"])

    assert result.exit_code == 0, result.output
    assert set(gh.get_issue(mine)["labels"]) == {
        "agent-reported",
        "fleet:ready",
        "class:bug",
        "fleet:auto-ok",
        "area:eval",
    }
    assert gh.get_issue(theirs)["labels"] == ["agent-reported"]


def test_run_new_run_mints_a_fresh_id_and_starts_at_zero(monkeypatch, tmp_path):
    """The escape hatch. Resuming is the safe default -- forgetting the night's
    spend hands the fleet a second full budget -- but a genuinely new night must
    not inherit the last one's total, or a fleet that spent $24 yesterday can
    never start today."""
    cfg = _run_cfg(tmp_path)
    RunLog(cfg.runlog_path, run_id="night-1").append(
        tick=1,
        at=datetime(2026, 8, 18, 2, 0, tzinfo=UTC),
        intent=Release(task=99, reason="stalled", attempt=2),
        outcome="applied",
        cost_usd=7.5,
    )
    _fake_backends(monkeypatch, cfg, FakeGh(), FakeHerdr(worktree_root=cfg.worktree_root))
    result = CliRunner().invoke(main, ["run", "--once", "--new-run"])
    assert result.exit_code == 0, result.output
    assert _run_id_of(result.output) != "night-1"
    assert "new run" in result.output
    assert "spent=$0.00" in result.output


def test_run_takes_an_explicit_run_id(monkeypatch, tmp_path):
    """A supervisor restarting the daemon can name the night itself, rather
    than depending on the log being where this process thinks it is."""
    cfg = _run_cfg(tmp_path)
    RunLog(cfg.runlog_path, run_id="night-1").append(
        tick=1,
        at=datetime(2026, 8, 18, 2, 0, tzinfo=UTC),
        intent=Release(task=99, reason="stalled", attempt=2),
        outcome="applied",
        cost_usd=7.5,
    )
    _fake_backends(monkeypatch, cfg, FakeGh(), FakeHerdr(worktree_root=cfg.worktree_root))
    result = CliRunner().invoke(main, ["run", "--once", "--run-id", "night-1"])
    assert result.exit_code == 0, result.output
    assert _run_id_of(result.output) == "night-1"
    assert "spent=$7.50" in result.output


def test_run_refuses_run_id_together_with_new_run(monkeypatch, tmp_path):
    """They contradict: one names a night to rejoin, the other says start a new
    one. Picking a winner silently is how an operator gets the opposite of what
    they asked for on the one command that decides whether the ceiling binds."""
    cfg = _run_cfg(tmp_path)
    _fake_backends(monkeypatch, cfg, FakeGh(), FakeHerdr(worktree_root=cfg.worktree_root))
    result = CliRunner().invoke(main, ["run", "--once", "--run-id", "x", "--new-run"])
    assert result.exit_code != 0
    assert "--new-run" in result.output


def test_run_with_an_empty_log_mints_an_id_and_says_it_is_new(monkeypatch, tmp_path):
    """Nothing to resume is not an error, and it must not read as a resume."""
    cfg = _run_cfg(tmp_path)
    _fake_backends(monkeypatch, cfg, FakeGh(), FakeHerdr(worktree_root=cfg.worktree_root))
    result = CliRunner().invoke(main, ["run", "--once"])
    assert result.exit_code == 0, result.output
    assert "new run" in result.output
    assert "resumed" not in result.output


def test_gh_login_survives_a_gh_that_never_answers(monkeypatch):
    """`_gh_login` runs before the first tick and its failure path already
    returns None -- a wrong assignee fails every label edit that rides along
    with it, so a guess is worse than nothing. A TimeoutExpired is not an
    OSError and not a CalledProcessError, so before the bound it escaped that
    handler and took `fleet run` down at startup with a traceback."""
    import fleet.cli as cli_module
    from fleet.cli import GH_LOGIN_TIMEOUT_SECONDS, _gh_login

    recorded = []

    def run(*args, **kwargs):
        recorded.append(kwargs)
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(cli_module.subprocess, "run", run)
    assert _gh_login() is None
    assert recorded[0]["timeout"] == GH_LOGIN_TIMEOUT_SECONDS > 0


def test_discover_refuses_an_area_the_target_repo_does_not_have(env, cfg):
    """The --area choice used to be the fixture tuple; now it is the target's
    ownership map, checked before anything is written. Catches validating
    against labels.AREAS, or not at all."""
    cfg.ownership.write_text("| Glob | Area |\n|---|---|\n| `src/**` | allocator |\n")
    env.setenv("FLEET_AGENT", "fleet-3")
    result = CliRunner().invoke(
        main, ["discover", "--from", "1", "--title", "t", "--area", "eval", "--evidence", "x:1"]
    )
    assert result.exit_code == 2
    assert "not an area" in result.output and "allocator" in result.output


def test_run_hands_the_daemon_a_composite_with_chain(monkeypatch, tmp_path):
    """`fleet run` is the one command that builds a ChainExecutor, and it must
    build it with the real `run_cli` looked up at call time (so the stand-in
    from `_fake_backends` is what it holds) and herdr's failures deque."""
    cfg = _run_cfg(tmp_path)
    _fake_backends(monkeypatch, cfg, FakeGh(), FakeHerdr(worktree_root=cfg.worktree_root))
    seen = {}

    def fake_tick(self, now):
        seen["executor"] = self.executor
        self.snapshot = FleetSnapshot(
            run_id=self.run_id,
            tick=1,
            at=now.isoformat(),
            rows=(),
            spent_usd=0.0,
            fleet_usd=self.policy.fleet_usd,
            free_slots=self.policy.slots,
            held_areas=(),
        )
        return self.snapshot

    monkeypatch.setattr(Daemon, "tick", fake_tick)
    result = CliRunner().invoke(main, ["run", "--once"])
    assert result.exit_code == 0, result.output
    executor = seen["executor"]
    assert isinstance(executor, CompositeExecutor)
    assert executor.chain is not None
    assert executor.chain.run_cli is chain_executor.run_cli
    assert executor.chain.run_cli(["chain", "status"])[0] == 127
    assert executor.chain.failures is executor.herdr.failures


def test_discover_passes_class_and_spec_through(monkeypatch, cfg):
    """`--class` defaults to bug and `--spec` reaches do_discover; any other
    class is refused by click before anything is filed."""
    seen = []
    monkeypatch.chdir(cfg.root)
    monkeypatch.setattr("fleet.cli.do_discover", lambda *a, **kw: (seen.append(kw), 7)[1])
    base = ["discover", "--from", "1", "--title", "t", "--area", "eval", "--evidence", "x:1"]

    assert CliRunner().invoke(main, base).exit_code == 0
    spec = ["--class", "build", "--spec", "docs/ai/specs/x.md"]
    assert CliRunner().invoke(main, [*base, *spec]).exit_code == 0
    refused = CliRunner().invoke(main, [*base, "--class", "spec"])

    assert [(kw["klass"], kw["spec"]) for kw in seen] == [
        ("bug", None),
        ("build", "docs/ai/specs/x.md"),
    ]
    assert refused.exit_code == 2
