"""Git facts the CLI needs. Read-only."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def head_sha(root: Path) -> str:
    return _git(root, "rev-parse", "--short=7", "HEAD")


def merge_base_sha(root: Path, base: str) -> str:
    return _git(root, "rev-parse", "--short=7", _git(root, "merge-base", "HEAD", base))


def branch_name(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD")
