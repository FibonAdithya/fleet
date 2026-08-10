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
