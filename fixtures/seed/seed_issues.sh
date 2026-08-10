#!/usr/bin/env bash
# Seed (or re-seed) the fixture backlog. Idempotent by title.
set -euo pipefail

REPO="${FLEET_FIXTURE_REPO:-FibonAdithya/fleet-fixture}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

uv run --with PyYAML python "$HERE/seed_issues.py" "$REPO" "$HERE/issues.yaml"
