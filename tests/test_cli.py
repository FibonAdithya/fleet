from __future__ import annotations

from types import SimpleNamespace

import click
import pytest

from conftest import git
from fleet.cli import _context, _git_facts
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
