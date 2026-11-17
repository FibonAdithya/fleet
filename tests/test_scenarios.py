from __future__ import annotations

from pathlib import Path

import pytest

from fleet.simulate import load_scenario, run

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = sorted((ROOT / "fixtures/scenarios").glob("*.yaml"))
EXPECTED_STEMS = {
    "area-exclusion",
    "blocked-self-resolves",
    "budget-drains",
    "chain-build-integrates",
    "done-closes-the-issue",
    "integration-lock",
    "ledger-drift",
    "orphaned-worktree",
    "poison-task",
    "rebase-storm",
    "stall-releases",
}


def test_the_scenario_set_is_exactly_the_files_this_task_names():
    """A glob that matches nothing makes the parametrized tests below vacuous --
    they report a pass having run zero scenarios. `>= 7` is not enough either:
    it passes with one file deleted and two added, which is how a failure-table
    row stops being covered without anything turning red."""
    assert {p.stem for p in SCENARIOS} == EXPECTED_STEMS


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenario(path):
    scenario = load_scenario(path)
    history = run(scenario, ROOT)
    # run() raises on the first tick whose expectation does not hold, so getting
    # here means every tick held. This asserts the other half -- that every tick
    # ran. Without it the test cannot tell "all expectations held" from "the
    # harness stopped after the first one".
    assert len(history) == len(scenario["ticks"])


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_every_scenario_asserts_at_least_one_thing_that_happened(path):
    """`expect: []` is a real assertion of absence, and an allocator that does
    nothing at all satisfies every one of them. A file made only of absences
    proves nothing about an allocator that works."""
    scenario = load_scenario(path)
    assert any(tick["expect"] for tick in scenario["ticks"])


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_every_scenario_cites_the_claim_it_proves(path):
    """Spec §7 makes `proves` the link between a file and the row it covers. A
    sentence citing nothing lets a scenario drift off the failure table with
    nothing to notice, which is the drift the YAML format exists to stop."""
    proves = load_scenario(path)["proves"]
    assert any(source in proves for source in ("parent §10", "parent §11", "harness"))
