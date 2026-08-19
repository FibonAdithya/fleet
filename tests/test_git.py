from __future__ import annotations

import subprocess

import pytest

from conftest import git
from fleet import git as fleet_git


def test_branch_name_reports_the_checked_out_branch(repo):
    assert fleet_git.branch_name(repo) == "main"


def test_a_handoff_refuses_a_detached_head(repo):
    """`rev-parse --abbrev-ref HEAD` returns the literal "HEAD" with exit 0, and
    the schema's `branch` only requires minLength 1 -- so it would validate and
    land in the audit trail as though it were a real branch. A reviewer has to
    be able to check the branch out."""
    git(repo, "checkout", "--detach", "HEAD")
    with pytest.raises(RuntimeError, match="detached HEAD"):
        fleet_git.branch_name(repo)


def test_escalate_and_block_stay_reachable_from_a_detached_head(repo):
    """They are the only paths that reach a human, and a detached HEAD --
    mid-bisect, mid-failed-rebase -- is exactly when an agent needs them.
    Refusing there left it with no command at all. The recorded value is the
    truth rather than a name, so anything trying to check it out fails loudly."""
    git(repo, "checkout", "--detach", "HEAD")
    name = fleet_git.branch_name(repo, allow_detached=True)
    assert name == f"detached at {fleet_git.head_sha(repo)}"
    assert name != "HEAD"


def test_merge_base_is_resolved_against_the_integration_branch(repo):
    assert fleet_git.merge_base_sha(repo, "integration") == fleet_git.head_sha(repo)


def test_a_hung_git_call_times_out(repo, monkeypatch):
    """`_git` shells out with no bound. `merge_base_sha` runs on every handoff,
    and a git that never returns -- an index lock held by a crashed process, a
    filesystem that has gone -- hangs the agent's `fleet handoff` forever with
    no output at all."""
    import fleet.git as git_module

    monkeypatch.setattr(git_module, "GIT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(
        git_module.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="git", timeout=kw["timeout"])
        ),
    )
    with pytest.raises(RuntimeError, match="timed out"):
        git_module.head_sha(repo)
