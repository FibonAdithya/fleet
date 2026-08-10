from __future__ import annotations

import pytest

from fleet.config import ConfigNotFound, load_config

TOML = """
[repo]
slug = "FibonAdithya/fleet-fixture"
integration_branch = "integration"

[policy]
ownership = "docs/agent/ownership.md"
task_classes = "docs/agent/task-classes.md"
roles = ".agents/roles"
schemas = ".agents/schemas"

[fleet]
slots = 6
stall_minutes = 10
max_attempts = 2
max_fix_passes = 1
"""


def _repo(tmp_path):
    (tmp_path / "fleet.toml").write_text(TOML)
    (tmp_path / ".agents" / "schemas").mkdir(parents=True)
    (tmp_path / "deep" / "nested").mkdir(parents=True)
    return tmp_path


def test_loads_from_repo_root(tmp_path):
    cfg = load_config(_repo(tmp_path))
    assert cfg.repo_slug == "FibonAdithya/fleet-fixture"
    assert cfg.integration_branch == "integration"
    assert cfg.slots == 6
    assert cfg.max_attempts == 2
    assert cfg.max_fix_passes == 1


def test_walks_up_from_a_subdirectory(tmp_path):
    root = _repo(tmp_path)
    cfg = load_config(root / "deep" / "nested")
    assert cfg.root == root


def test_policy_paths_are_absolute(tmp_path):
    cfg = load_config(_repo(tmp_path))
    assert cfg.schemas_dir.is_absolute()
    assert cfg.schemas_dir == tmp_path / ".agents" / "schemas"


def test_missing_config_raises(tmp_path):
    with pytest.raises(ConfigNotFound):
        load_config(tmp_path)
