#!/usr/bin/env bash
# Undo what a graded run does to the fixture repo, so the gate can be run
# again. Mirrors the hand-run steps docs/runbook.md's "Resetting after a
# graded run" documents, minus the ledger capture -- that step reads real
# state and belongs to the caller, not this script; see the warning below.
#
# CAPTURE THE LEDGER BEFORE RUNNING THIS. Once this script runs, the state a
# graded run left behind is gone -- comments deleted, labels stripped,
# branches removed. docs/runbook.md's own warning:
#
#     Capture the ledger BEFORE anything below runs, or the snapshot records a
#     state no live run produces:
#
#         gh issue list --repo FibonAdithya/fleet-fixture --state all \
#           --limit 200 --json assignees,body,labels,number,state,title \
#           > fixtures/ledger/issues.json
#
# This script does not do that capture for you.
set -euo pipefail

REPO="${FLEET_FIXTURE_REPO:-FibonAdithya/fleet-fixture}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
CLONE="${FLEET_FIXTURE_CLONE:-$HOME/TIG/fleet-fixture}"

# This script mutates real GitHub issues irreversibly -- deletes comments,
# strips labels, closes and reopens issues -- and a human triggers it by hand.
# Unlike seed_issues.sh (idempotent, additive, safe to re-run blind), a wrong
# invocation here leaves a fixture repo in a state nobody can reconstruct.
# `--yes` or `FORCE=1` skips the prompt for scripted/gate use.
CONFIRMED=0
for arg in "$@"; do
  case "$arg" in
    --yes) CONFIRMED=1 ;;
  esac
done
if [ "${FORCE:-0}" = "1" ]; then
  CONFIRMED=1
fi
if [ "$CONFIRMED" -ne 1 ]; then
  read -r -p "About to reset $REPO: delete run comments, strip labels, close/reopen issues. Type 'yes' to continue: " REPLY
  if [ "$REPLY" != "yes" ]; then
    echo "Aborted -- nothing was touched." >&2
    exit 1
  fi
fi

echo "Resetting $REPO. Did you capture fixtures/ledger/issues.json first? (see this script's header)" >&2

# --- Issue-side reset: labels, comments, assignees, open/closed state -------
# Run from the project root, not the caller's cwd -- reset.py imports fleet.gh
# and fleet.labels, so it needs this project's environment rather than
# whatever `uv run` would resolve elsewhere. Same reasoning as seed_issues.sh.
#
# `-m fixtures.seed.reset`, NOT `python "$HERE/reset.py"`: running a script by
# path sets sys.path[0] to the script's own directory (fixtures/seed/), never
# the CWD, so reset.py's `from fixtures.seed.seed_issues import issue_labels`
# could not resolve -- reproduced live: `uv run python fixtures/seed/reset.py`
# from $ROOT fails with `ModuleNotFoundError: No module named 'fixtures'`
# every time, before ever constructing a client. `-m` runs the module with the
# CWD on sys.path instead, which is exactly what running from `$ROOT` needs.
cd "$ROOT"
uv run python -m fixtures.seed.reset "$REPO" "$HERE/issues.yaml"

# --- Label-side cleanup: discovered-by:* labels ------------------------------
# `discovered-by:<n>` is generated at runtime by do_discover and is not part
# of the static vocabulary, so it is never in `seeded`'s target labels and
# reset.py already strips it off every one of the eleven it appears on. But
# deleting the label outright removes it from the run's discovered issues too
# (which reset.py has already closed, not relabelled) and stops it
# accumulating across many graded runs.
# `grep` wrapped in `{ ... || true; }`: under `set -o pipefail`, zero matches
# -- the common case on a run that never discovered anything -- makes grep
# exit 1, which would abort the whole script here before the git-side reset
# below ever runs. `xargs -r` already treats empty input as a no-op, which is
# all that should happen when there is nothing to delete.
gh label list --repo "$REPO" --limit 200 --json name --jq '.[].name' \
  | { grep '^discovered-by:' || true; } \
  | xargs -r -I{} gh label delete {} --repo "$REPO" --yes

# --- Git-side reset: integration branch, fleet/* branches, worktrees --------
# Only attempted if the fixture repo is actually checked out locally -- a
# fresh environment with no clone yet has nothing to reset here, and the next
# `git clone` starts from `main` regardless.
if [ -d "$CLONE/.git" ]; then
  (
    cd "$CLONE"
    # LOCAL ONLY, and that is a limit rather than a design. This script
    # resets refs inside this one clone and pushes nothing.
    #
    # The old comment here said "nothing in this project ever runs `git push`
    # or `git fetch`". The push half is still true. The fetch half is not:
    # src/fleet/herdr.py's create_worktree runs `git fetch origin <base>` and
    # branches every new worktree off `origin/<base>`, which is spec §8.2's
    # fresh-base rule. So the branch a graded run actually starts from is
    # `origin/integration`, and THIS LINE CANNOT RESET IT.
    #
    # What the line below does do: move the local `integration` ref back to
    # `main`, so a run's rebases and merges do not survive into the next one
    # and run 2's integrator does not start mid-integration. `branch -f`
    # moves the ref without a checkout of its own.
    #
    # What a human still has to do, if run 1 pushed anything (docs/m3-inputs.md
    # §1.1 and §1.3):
    #
    #     git -C "$CLONE" push --force-with-lease origin integration:integration
    #
    # Not done here on purpose. Pushing is outward-facing, and a cleanup script
    # that force-pushes a remote branch is a much worse failure than one that
    # leaves a human a line to run.
    git branch -f integration main
    # Can't delete a branch that is currently checked out -- switch to main
    # first, the same order docs/runbook.md's manual reset already uses.
    git checkout main
    git branch -D $(git branch --list 'fleet/*') 2>/dev/null || true
    # Worktrees live under [daemon] worktrees (default ~/.fleet/worktrees,
    # outside this repo on purpose -- see docs/runbook.md), so removing a
    # fleet/*-named one has to go by name, not by checking this repo's
    # worktree list for a path prefix that may not match.
    git worktree list --porcelain \
      | awk '/^worktree /{path=$2} /^branch refs\/heads\/fleet\//{print path}' \
      | xargs -r -I{} git worktree remove --force {}
    git worktree prune
  )
else
  echo "no local clone at $CLONE -- skipping the git-side reset" >&2
fi

echo "Reset complete: issues 1-11 carry their seeded labels again, everything" >&2
echo "past 11 is closed, and the LOCAL integration/fleet:*/worktrees are back" >&2
echo "to main. No re-seed needed -- reset.py restores labels directly rather" >&2
echo "than relying on seed_issues.sh's idempotent-by-title re-run." >&2
echo >&2
echo "NOT reset, because this script never pushes: origin/integration, which is" >&2
echo "the ref create_worktree actually branches from (spec §8.2). If run 1" >&2
echo "pushed it, reset it by hand before run 2 -- see docs/m3-inputs.md §1.3:" >&2
echo "  git -C $CLONE push --force-with-lease origin integration:integration" >&2
