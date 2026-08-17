from __future__ import annotations

import pytest

from fleet.commands import DuplicateDiscovery, do_discover
from fleet.gh import FakeGh


@pytest.fixture()
def gh():
    client = FakeGh()
    client.create_issue(
        "estimate_lid should accept an explicit k",
        "body",
        ["fleet:claimed", "area:eval", "fleet:auto-ok"],
    )
    return client


def test_discovery_lands_in_triage_not_ready(cfg, gh):
    n = do_discover(
        cfg,
        gh,
        parent=1,
        title="make_synthetic shifts only dim 0",
        area="data",
        evidence="src/data/synth.py:31 sets offset[0] only",
    )
    names = gh.get_issue(n)["labels"]
    assert "fleet:triage" in names
    assert "fleet:ready" not in names


def test_discovery_never_gets_auto_ok(cfg, gh):
    n = do_discover(
        cfg,
        gh,
        parent=1,
        title="make_synthetic shifts only dim 0",
        area="data",
        evidence="src/data/synth.py:31",
    )
    assert "fleet:auto-ok" not in gh.get_issue(n)["labels"]


def test_discovery_is_linked_to_its_parent(cfg, gh):
    n = do_discover(
        cfg,
        gh,
        parent=1,
        title="make_synthetic shifts only dim 0",
        area="data",
        evidence="src/data/synth.py:31",
    )
    assert "discovered-by:1" in gh.get_issue(n)["labels"]
    assert "#1" in gh.get_issue(n)["body"]


def test_evidence_is_recorded_in_the_body(cfg, gh):
    n = do_discover(
        cfg,
        gh,
        parent=1,
        title="make_synthetic ignores dims above 0",
        area="data",
        evidence="src/data/synth.py:31 sets offset[0] only",
    )
    assert "offset[0] only" in gh.get_issue(n)["body"]


def test_parent_state_is_untouched(cfg, gh):
    before = list(gh.get_issue(1)["labels"])
    do_discover(
        cfg, gh, parent=1, title="make_synthetic ignores dims above 0", area="data", evidence="e"
    )
    assert gh.get_issue(1)["labels"] == before


def test_duplicate_title_is_refused(cfg, gh):
    do_discover(
        cfg, gh, parent=1, title="make_synthetic shifts only dim 0", area="data", evidence="e"
    )
    with pytest.raises(DuplicateDiscovery):
        do_discover(
            cfg, gh, parent=1, title="MAKE_SYNTHETIC shifts only dim 0", area="data", evidence="e"
        )


def test_a_blank_title_is_refused(cfg, gh):
    """A whitespace-only title also defeats the duplicate guard: search_titles("")
    matches every issue while the exact-match test matches none, so the scan
    always passes and every blank discovery files another untitled issue."""
    before = len(gh.list_issues("all"))
    with pytest.raises(ValueError, match="title"):
        do_discover(cfg, gh, parent=1, title="   ", area="data", evidence="e")
    assert len(gh.list_issues("all")) == before


def test_unknown_area_is_rejected(cfg, gh):
    with pytest.raises(ValueError):
        do_discover(cfg, gh, parent=1, title="anything", area="nonsense", evidence="e")


def test_a_substring_collision_is_not_a_duplicate(cfg, gh):
    """The parent issue is titled "estimate_lid should accept an explicit k".
    A bare substring check refuses any title contained in it, "k" included."""
    n = do_discover(cfg, gh, parent=1, title="k", area="data", evidence="e")
    assert gh.get_issue(n)["title"] == "k"


def test_the_parentage_label_is_created_before_it_is_applied(gh, cfg):
    """`discovered-by:<n>` is generated at runtime and is not in the repo's
    label set. Real gh refuses an unknown label and creates no issue at all, and
    FakeGh now mirrors that -- so inverting the order fails this test rather
    than passing it silently."""
    do_discover(
        cfg, gh, parent=1, title="make_synthetic ignores dims above 0", area="data", evidence="e"
    )
    assert "discovered-by:1" in gh.ensured_labels
