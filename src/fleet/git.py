"""Git facts the CLI needs. Read-only."""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Seconds any one git call may take. Every call here is a local read --
#: rev-parse, merge-base -- with no network in it, so 30 is far past any
#: legitimate one. It is a bound on the pathological case: an index lock held
#: by a crashed process, or a filesystem that has gone away, either of which
#: hangs an agent's `fleet handoff` forever with no output at all.
GIT_TIMEOUT_SECONDS = 30


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_SECONDS}s") from exc
    except subprocess.CalledProcessError as exc:
        # Same reasoning as RealGh._run: CalledProcessError carries argv and exit
        # code but not stderr, which is where git says "not a valid object name".
        raise RuntimeError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc


def head_sha(root: Path) -> str:
    return _git(root, "rev-parse", "--short=7", "HEAD")


def merge_base_sha(root: Path, base: str) -> str:
    return _git(root, "rev-parse", "--short=7", _git(root, "merge-base", "HEAD", base))


def branch_name(root: Path, *, allow_detached: bool = False) -> str:
    name = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if name == "HEAD":
        # Detached HEAD returns the literal string "HEAD" with exit 0. The schema's
        # `branch` only requires minLength 1, so it would validate and land in the
        # permanent audit trail as though it were a real branch.
        if not allow_detached:
            raise RuntimeError("detached HEAD: check out a branch before handing off")
        # escalate and block are the only paths that reach a human, and a broken
        # git state is exactly when an agent needs them. Refusing there would leave
        # it with no command at all. Record the truth instead of a name: anything
        # downstream that tries to check this out fails loudly rather than
        # resolving to the wrong branch, which "HEAD" would.
        return f"detached at {head_sha(root)}"
    return name


def exists_on(root: Path, ref: str, path: str) -> bool:
    """Whether `path` is a file committed at `ref` (`git cat-file -e`). A git
    failure of any kind, a missing ref included, reads as absent: every caller
    refuses on absent, so an unreadable repo refuses rather than proceeds."""
    try:
        _git(root, "cat-file", "-e", f"{ref}:{path}")
    except RuntimeError:
        return False
    return True
