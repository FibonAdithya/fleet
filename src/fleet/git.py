"""Git facts the CLI needs. Read-only."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        # Same reasoning as RealGh._run: CalledProcessError carries argv and exit
        # code but not stderr, which is where git says "not a valid object name".
        raise RuntimeError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc


def head_sha(root: Path) -> str:
    return _git(root, "rev-parse", "--short=7", "HEAD")


def merge_base_sha(root: Path, base: str) -> str:
    return _git(root, "rev-parse", "--short=7", _git(root, "merge-base", "HEAD", base))


def branch_name(root: Path) -> str:
    name = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if name == "HEAD":
        # Detached HEAD returns the literal string "HEAD" with exit 0. The schema's
        # `branch` only requires minLength 1, so it would validate and land in the
        # permanent audit trail as though it were a real branch.
        raise RuntimeError("detached HEAD: check out a branch before handing off")
    return name
