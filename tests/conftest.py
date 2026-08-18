"""Fixtures shared across the suite.

`cfg` used to be copy-pasted verbatim into three modules, hardcoded path and
all. Changing fleet.toml's shape then meant editing three places, and whichever
one was missed kept passing against a stale config.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from fleet.config import load_config

# Vendored, not a live path into another repo's working tree. See
# fixtures/policy/README.md for why that distinction cost us a merge.
FIXTURE_POLICY = Path(__file__).resolve().parents[1] / "fixtures/policy/fleet-fixture"
FIXTURE_SCHEMAS = FIXTURE_POLICY / ".agents/schemas"

FLEET_TOML = (
    '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
    'roles = ".agents/roles"\nschemas = ".agents/schemas"\n'
)


@pytest.fixture()
def cfg(tmp_path):
    (tmp_path / "fleet.toml").write_text(FLEET_TOML)
    shutil.copytree(FIXTURE_SCHEMAS, tmp_path / ".agents" / "schemas")
    return load_config(tmp_path)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    """A one-commit repo with an `integration` branch, for the git seam.

    fleet reads three facts from git and one of them -- branch_name -- behaves
    differently on a detached HEAD, which is not something a mock can tell you.
    """
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "f.txt").write_text("one\n")
    git(tmp_path, "add", "f.txt")
    git(tmp_path, "commit", "-m", "one")
    git(tmp_path, "branch", "integration")
    return tmp_path
