"""The seeder is what stands a target repo up from nothing, so it is the one
place where "apply a label that does not exist" is guaranteed to happen. FakeGh
refuses an uncreated label exactly as gh does, which makes it a real test of
that ordering rather than a mock of it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from fleet.gh import FakeGh
from fleet.labels import static_vocabulary

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "fixtures/seed/issues.yaml"

_spec = importlib.util.spec_from_file_location("seed_issues", ROOT / "fixtures/seed/seed_issues.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def test_seeding_a_bare_repo_creates_every_issue():
    """`gh issue create --label X` does not create X -- it fails and creates no
    issue at all. Applying the labels without creating them first aborted on the
    very first issue and left the backlog empty, so "idempotent by title, safe
    to re-run" held only on a repo somebody had already labelled by hand."""
    gh = FakeGh()
    assert seed.main("o/r", str(SPEC), gh) == 0
    assert len(gh.list_issues("all")) == len(yaml.safe_load(SPEC.read_text()))


def test_seeding_creates_the_whole_vocabulary_not_just_what_it_applies():
    """Keeping the repo's label set sourced from static_vocabulary is what stops
    it drifting from the counters fleet.toml can move."""
    gh = FakeGh()
    seed.main("o/r", str(SPEC), gh)
    assert static_vocabulary() <= gh.ensured_labels


def test_seeding_twice_is_a_no_op():
    gh = FakeGh()
    seed.main("o/r", str(SPEC), gh)
    before = [i["number"] for i in gh.list_issues("all")]
    seed.main("o/r", str(SPEC), gh)
    assert [i["number"] for i in gh.list_issues("all")] == before


def test_every_seeded_issue_lands_ready_with_its_area_and_class():
    gh = FakeGh()
    seed.main("o/r", str(SPEC), gh)
    for spec, row in zip(yaml.safe_load(SPEC.read_text()), gh.list_issues("all"), strict=True):
        assert row["title"] == spec["title"]
        assert "fleet:ready" in row["labels"]
        assert f"area:{spec['area']}" in row["labels"]
        assert f"class:{spec['class']}" in row["labels"]
        assert ("fleet:auto-ok" in row["labels"]) == bool(spec.get("auto_ok"))


def test_an_unknown_area_in_the_spec_is_named_rather_than_sent_to_github(tmp_path):
    """Building the label names through the vocabulary instead of f-strings
    turns a typo in issues.yaml into a message naming the value, rather than an
    opaque "could not add label" from gh half way through a seeding run."""
    bad = tmp_path / "issues.yaml"
    bad.write_text(
        yaml.safe_dump([{"key": "x", "title": "t", "body": "b", "area": "evla", "class": "patch"}])
    )
    gh = FakeGh()
    with pytest.raises(ValueError, match="evla"):
        seed.main("o/r", str(bad), gh)
    assert gh.list_issues("all") == []
