"""This repository is also a fleet target. Its policy layer must load, and its
areas must cover every source file, or a task in an uncovered file is excluded
from nothing."""

from __future__ import annotations

from pathlib import Path

from fleet.config import load_config
from fleet.policy import load

ROOT = Path(__file__).resolve().parents[1]


def test_the_repository_policy_loads():
    policy = load(load_config(ROOT))
    assert policy.area_names == frozenset(
        {"allocator", "ledger", "cost", "executor", "daemon", "cli", "docs"}
    )
    assert policy.slots == 3
    assert policy.fleet_usd == 10.0


def test_every_source_file_has_an_area_or_is_non_dispatchable():
    """Catches adding a module to src/fleet without mapping it. The
    non-dispatchable files are listed here on purpose: a new one has to be
    added deliberately, in two places."""
    non_dispatchable = {"labels.py", "packet.py", "commands.py", "schemas.py", "__init__.py"}
    policy = load(load_config(ROOT))
    globs = [glob for glob, _ in policy.areas]
    for path in sorted((ROOT / "src" / "fleet").glob("*.py")):
        if path.name in non_dispatchable:
            continue
        rel = path.relative_to(ROOT).as_posix()
        assert any(Path(rel).match(g) for g in globs), f"{rel} has no area"


def test_the_repository_policy_routes_build_and_bug_to_chain():
    policy = load(load_config(ROOT))
    assert policy.route_for("build") == ("chain", "integrator")
    assert policy.route_for("bug") == ("chain", "integrator")


def test_repository_handoff_schema_accepts_chain_role():
    """Catches `chain` missing from the root handoff schema's from.role enum."""
    from fleet.schemas import validate

    packet = {
        "schema": "fleet.handoff.v1",
        "task": 1,
        "to": "review",
        "attempt": 1,
        "from": {"role": "chain", "agent": "chain-1", "run": "2026-11-17T00:00:00Z"},
        "base_sha": "fb5ec5d",
        "head_sha": "a91c204",
        "branch": "fleet/1-x",
        "did": "built it",
        "evidence": {"checks": "make check: pass", "commands": ["make check"]},
    }
    validate(load_config(ROOT), "handoff", packet)
