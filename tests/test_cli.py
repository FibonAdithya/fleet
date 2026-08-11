from __future__ import annotations

from types import SimpleNamespace

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
    _, _, identity = _context()
    assert identity["agent"] == "manual"
    assert "FLEET_AGENT unset" in capsys.readouterr().err


def test_an_exported_but_empty_fleet_role_falls_back_to_implementer(env):
    env.setenv("FLEET_AGENT", "fleet-3")
    env.setenv("FLEET_ROLE", "")
    _, _, identity = _context()
    assert identity["role"] == "implementer"


def test_a_real_agent_id_is_used_and_warns_about_nothing(env, capsys):
    env.setenv("FLEET_AGENT", "fleet-3")
    _, _, identity = _context()
    assert identity["agent"] == "fleet-3"
    assert capsys.readouterr().err == ""


def test_the_generated_run_timestamp_satisfies_the_schema(cfg, env):
    """The CLI formats `run` itself, and the schema's date-time format is now
    enforced -- so a change to that strftime has to fail here."""
    env.setenv("FLEET_AGENT", "fleet-3")
    _, _, identity = _context()
    validate(
        cfg,
        "handoff",
        {
            "schema": "fleet.handoff.v1",
            "task": 1,
            "to": "review",
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
