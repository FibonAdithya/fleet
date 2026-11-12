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
#         gh issue list --repo "${FLEET_FIXTURE_REPO:?set it to <you>/fleet-fixture}" --state all \
#           --limit 200 --json assignees,body,labels,number,state,title \
#           > fixtures/ledger/issues.json
#
# This script does not do that capture for you. The capture line requires
# FLEET_FIXTURE_REPO while this script defaults REPO to the owner's repository:
# a capture pasted against the wrong repository silently records the wrong
# ledger, whereas this script names $REPO in its confirmation prompt before it
# touches anything.
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
# Delegated to reset_git.sh, which checks every step, verifies the end state,
# and answers non-zero if any of it did not hold. This block used to swallow
# every failure (`2>/dev/null || true`, an unchecked xargs) and the epilogue
# below printed "Reset complete" regardless -- so a reset that left run 1's
# fleet/* branches behind was indistinguishable from one that worked, and run 2
# started its implementer on run 1's commits. A false green on the gate is
# worse than a red, because a red stops the run.
#
# Only attempted if the fixture repo is actually checked out locally -- a fresh
# environment with no clone yet has nothing to reset here, and the next
# `git clone` starts from `main` regardless.
GIT_OK=1
GIT_SKIPPED=0
if [ -d "$CLONE/.git" ]; then
  if ! bash "$HERE/reset_git.sh" "$CLONE"; then
    GIT_OK=0
  fi
else
  GIT_SKIPPED=1
  echo "no local clone at $CLONE -- skipping the git-side reset" >&2
fi

if [ "$GIT_OK" -ne 1 ]; then
  echo >&2
  echo "Reset INCOMPLETE. The issue-side reset ran, but the git-side reset" >&2
  echo "reported the failures above and the clone at $CLONE is NOT clean." >&2
  echo "Do not start another graded run until it is: a surviving fleet/* branch" >&2
  echo "makes _existing_branch skip --base, which starts the next run's" >&2
  echo "implementer on this run's commits -- a green that means nothing." >&2
  exit 1
fi

echo "Reset complete: issues 1-11 carry their seeded labels again, everything" >&2
echo "past 11 is closed. No re-seed needed -- reset.py restores labels directly" >&2
echo "rather than relying on seed_issues.sh's idempotent-by-title re-run." >&2
if [ "$GIT_SKIPPED" -eq 1 ]; then
  echo >&2
  echo "The git-side reset was SKIPPED: there is no clone at $CLONE. Nothing" >&2
  echo "local needed resetting; the next clone starts from main." >&2
else
  echo "The LOCAL integration ref, fleet/* branches and their worktrees are" >&2
  echo "gone, verified rather than assumed." >&2
fi
echo >&2
echo "NOT reset, because this script never pushes: origin/integration, which is" >&2
echo "the ref create_worktree actually branches from (spec §8.2). If run 1" >&2
echo "pushed it, reset it by hand before run 2 -- see docs/m3-inputs.md §1.3:" >&2
echo "  git -C $CLONE push --force-with-lease origin integration:integration" >&2
