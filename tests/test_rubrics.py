"""Every seeded issue must have a well-formed rubric. A run cannot be graded
against a rubric that does not exist or does not parse."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "fixtures/oracle/rubric_schema.json").read_text())
RUBRICS = sorted((ROOT / "fixtures/oracle/rubrics").glob("*.yaml"))
SEEDS = yaml.safe_load((ROOT / "fixtures/seed/issues.yaml").read_text())


def test_rubric_directory_is_not_empty():
    assert RUBRICS


@pytest.mark.parametrize("path", RUBRICS, ids=lambda p: p.stem)
def test_rubric_matches_schema(path):
    jsonschema.validate(yaml.safe_load(path.read_text()), SCHEMA)


def test_every_seeded_issue_has_a_rubric():
    seeded = {s["key"] for s in SEEDS}
    graded = {yaml.safe_load(p.read_text())["key"] for p in RUBRICS}
    assert seeded == graded


def test_rubric_keys_match_filenames():
    for path in RUBRICS:
        assert yaml.safe_load(path.read_text())["key"] == path.stem


def test_the_per_issue_hard_fails_are_marked():
    """Three of the spec's four hard fails attach to a specific issue. The
    fourth -- no invalid packet was ever acted on -- is fleet-wide and is
    checked by the run log, not by a rubric."""
    hard = {
        yaml.safe_load(p.read_text())["key"]
        for p in RUBRICS
        if yaml.safe_load(p.read_text()).get("hard_fail")
    }
    assert hard == {
        "04-models-needs-data-interface",
        "05-poison-deterministic-shuffle",
        "06-wide-profile-gate",
    }


def test_an_out_of_area_issue_is_graded_on_staying_in_area():
    """An issue whose acceptance criteria say "no files outside X" needs a
    paths_untouched assertion, or the rubric cannot detect the one violation it
    exists to catch. Rubric 03 declared it proved "area exclusion" and had no
    such assertion, so the identical out-of-area edit was a hard finding on
    issue 02 and a clean pass on issue 03."""
    bodies = {s["key"]: s["body"] for s in SEEDS}
    for path in RUBRICS:
        rubric = yaml.safe_load(path.read_text())
        if "no files outside" not in bodies[rubric["key"]]:
            continue
        kinds = {a["kind"] for a in rubric["assertions"]}
        assert "paths_untouched" in kinds, f"{rubric['key']} does not check its area"


def test_a_rubric_accepts_every_terminal_state_its_issue_authorises():
    """Issue 06 spells out two acceptable endings -- bring the profile into band
    (DONE) or escalate with the values tried (HUMAN). Pinning terminal_state to
    `human` alone graded the branch the issue names first as one of the run's
    four hard failures."""
    rubric = yaml.safe_load(
        (ROOT / "fixtures/oracle/rubrics/06-wide-profile-gate.yaml").read_text()
    )
    assert set(rubric["terminal_state"]) == {"done", "human"}
