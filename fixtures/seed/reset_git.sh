#!/usr/bin/env bash
# The git-side half of the fixture reset: worktrees, fleet/* branches, and the
# LOCAL integration ref. Called by reset.sh; a separate file because it is the
# half that can be exercised offline against a throwaway clone, and it is the
# half whose silent failure is worse than a loud one.
#
# Exit status is the point of this script. It answers 0 only when every step
# held AND the end state is what it claims, and names every failure on stderr.
# The version this replaces ran `git branch -D $(...) 2>/dev/null || true`,
# ignored each worktree removal, and its caller printed "Reset complete"
# regardless -- so a reset that left run 1's `fleet/*` branches behind looked
# identical to one that worked. Run 2's `RealHerdr._existing_branch` then finds
# those branches, skips `--base`, and starts run 2's implementer on run 1's
# commits. That is a FALSE GREEN on the gate, which is worse than a red: a red
# stops the run.
#
# LOCAL ONLY. Nothing here pushes, so `origin/integration` -- the ref
# create_worktree actually branches from (spec §8.2) -- is untouched. See
# docs/m3-inputs.md §1.3 for the one line a human runs.

# NOT `set -e`: every step below is checked explicitly, and the point is to
# report ALL of them rather than to stop at the first. `pipefail` still matters
# for the pipelines that feed the loops.
set -uo pipefail

CLONE="${1:?usage: reset_git.sh <clone-path>}"
FAILED=0

fail() {
  echo "FAILED: $*" >&2
  FAILED=1
}

if ! cd "$CLONE" 2>/dev/null; then
  echo "FAILED: no directory at $CLONE" >&2
  exit 1
fi
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  # A mistyped FLEET_FIXTURE_CLONE would otherwise reset nothing and report
  # success, which is the same false green by another route.
  echo "FAILED: $CLONE is not a git repository" >&2
  exit 1
fi

# 1. Worktrees FIRST, and that order is the fix, not a preference. git refuses
#    to delete a branch that is checked out in a linked worktree ("used by
#    worktree at ..."), and a graded run leaves exactly that shape: a
#    `fleet/N-slug` branch with the run's worktree still on it. Deleting
#    branches first therefore failed on every single one.
#
#    Worktrees live under [daemon] worktrees (default ~/.fleet/worktrees,
#    outside this repo on purpose -- see docs/runbook.md), so they are found by
#    the branch they are on rather than by a path prefix that may not match.
#
#    Process substitution rather than a pipe: a piped `while` runs in a subshell
#    and every `fail` in it would be lost at the closing `done`.
while IFS= read -r path; do
  [ -n "$path" ] || continue
  if ! git worktree remove --force "$path"; then
    fail "could not remove worktree $path"
  fi
done < <(git worktree list --porcelain |
  awk '/^worktree /{p=$2} /^branch refs\/heads\/fleet\//{print p}')
git worktree prune || fail "could not prune worktrees"

# 2. Can't delete a branch that is currently checked out, so land on main first
#    -- the same order docs/runbook.md's manual reset already uses.
git checkout main || fail "could not check out main"

# 3. The LOCAL integration ref back to main. `branch -f` moves it without a
#    checkout of its own. A run's rebases and merges must not survive a reset,
#    or run 2's integrator starts mid-integration rather than from main.
git branch -f integration main || fail "could not reset the local integration ref to main"

# 4. The run's branches.
while IFS= read -r branch; do
  [ -n "$branch" ] || continue
  if ! git branch -D "$branch"; then
    fail "could not delete $branch"
  fi
done < <(git branch --list 'fleet/*' --format='%(refname:short)')

# 5. Verify rather than assume. This is the assertion the gate depends on: a
#    surviving fleet/* branch is what makes run 2 start on run 1's commits, and
#    every step above can fail for a reason nobody predicted.
remaining="$(git branch --list 'fleet/*' --format='%(refname:short)' | tr '\n' ' ')"
remaining="${remaining% }"
if [ -n "$remaining" ]; then
  fail "fleet/* branches survived the reset: $remaining"
  echo "  Run 2 must not start: _existing_branch will find these, skip --base," >&2
  echo "  and put run 2's implementer on run 1's commits." >&2
fi

exit "$FAILED"
