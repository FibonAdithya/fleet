from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from conftest import FIXTURE_POLICY, git
from fleet.cli import ReadOnlyGh, _context, _git_facts, main
from fleet.config import load_config
from fleet.daemon import Daemon
from fleet.gh import RealGh
from fleet.model import FleetSnapshot
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


# -- `fleet plan --dry-run` / `--no-dry-run` -------------------------------


def test_plan_dry_run_wraps_the_client_in_read_only(monkeypatch, cfg):
    """The default. A caller who never says `--no-dry-run` must still get a
    client that refuses every write."""
    monkeypatch.chdir(cfg.root)
    seen = {}
    monkeypatch.setattr("fleet.cli.do_plan", lambda cfg_, gh, now: seen.setdefault("gh", gh) and [])
    result = CliRunner().invoke(main, ["plan"])
    assert result.exit_code == 0, result.output
    assert isinstance(seen["gh"], ReadOnlyGh)


def test_plan_no_dry_run_selects_the_writing_client(monkeypatch, cfg):
    """Ruling D6: the plan deletes the M3 refusal but left `plan()`'s body
    pinned to `ReadOnlyGh`, so `--no-dry-run` would be accepted and silently do
    nothing -- the worst of both worlds. This pins that `--no-dry-run` actually
    swaps in the unwrapped, writing client rather than leaving it a no-op flag."""
    monkeypatch.chdir(cfg.root)
    seen = {}
    monkeypatch.setattr("fleet.cli.do_plan", lambda cfg_, gh, now: seen.setdefault("gh", gh) and [])
    result = CliRunner().invoke(main, ["plan", "--no-dry-run"])
    assert result.exit_code == 0, result.output
    assert type(seen["gh"]) is RealGh
    assert not isinstance(seen["gh"], ReadOnlyGh)


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
