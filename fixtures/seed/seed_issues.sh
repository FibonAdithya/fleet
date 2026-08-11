#!/usr/bin/env bash
# Seed (or re-seed) the fixture repo's labels and backlog. Idempotent.
set -euo pipefail

REPO="${FLEET_FIXTURE_REPO:-FibonAdithya/fleet-fixture}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

# Run from the project root, not the caller's cwd: the seeder imports fleet.gh
# for ensure_label and fleet.labels for the vocabulary, so it needs this
# project's environment rather than whatever `uv run` would resolve elsewhere.
cd "$ROOT"
uv run python "$HERE/seed_issues.py" "$REPO" "$HERE/issues.yaml"
