from __future__ import annotations

import pytest

from fleet.commands import do_block, do_escalate, do_handoff
from fleet.gh import FakeGh, GhError
from fleet.packet import parse_latest
from fleet.schemas import SchemaError

BASE = dict(
    role="implementer",
    agent="fleet-3",
    run="2026-08-10T02:14:00Z",
    base_sha="fb5ec5d",
    head_sha="a91c204",
    branch="fleet/21-lid",
    did="Clamped neighbour_count to at least 1",
    checks="make check: pass",
    commands=["make check"],
)


@pytest.fixture()
def gh():
    client = FakeGh()
    client.create_issue("estimate_lid crashes", "body", ["fleet:claimed", "area:eval"])
    client.create_issue("add make_reference", "body", ["fleet:ready", "area:data"])
    return client


def test_handoff_posts_a_parseable_packet(cfg, gh):
    do_handoff(cfg, gh, task=1, to="review", **BASE)
    packet = parse_latest(gh.get_issue(1)["comments"], "handoff")
    assert packet["to"] == "review"
    assert packet["did"].startswith("Clamped")


def test_handoff_moves_the_state_label(cfg, gh):
    do_handoff(cfg, gh, task=1, to="review", **BASE)
    names = gh.get_issue(1)["labels"]
    assert "fleet:review" in names
    assert "fleet:claimed" not in names
    assert "area:eval" in names


def test_handoff_carries_do_not_forward(cfg, gh):
    do_handoff(cfg, gh, task=1, to="review", do_not=["Do not change the gate band."], **BASE)
    packet = parse_latest(gh.get_issue(1)["comments"], "handoff")
    assert packet["do_not"] == ["Do not change the gate band."]


def test_invalid_packet_writes_nothing(cfg, gh):
    bad = {**BASE, "head_sha": "HEAD"}
    with pytest.raises(SchemaError):
        do_handoff(cfg, gh, task=1, to="review", **bad)
    assert gh.get_issue(1)["comments"] == []
    assert "fleet:claimed" in gh.get_issue(1)["labels"]


def test_empty_did_is_rejected_before_writing(cfg, gh):
    with pytest.raises(SchemaError):
        do_handoff(cfg, gh, task=1, to="review", **{**BASE, "did": ""})
    assert gh.get_issue(1)["comments"] == []


def test_block_sets_blocked_state_and_records_the_dependency(cfg, gh):
    do_block(cfg, gh, task=1, on=2, **BASE)
    packet = parse_latest(gh.get_issue(1)["comments"], "handoff")
    assert packet["blocked_on"] == 2
    assert packet["to"] == "blocked"
    assert "fleet:blocked" in gh.get_issue(1)["labels"]


def test_block_on_a_nonexistent_issue_raises_and_writes_nothing(cfg, gh):
    with pytest.raises(GhError):
        do_block(cfg, gh, task=1, on=99, **BASE)
    assert gh.get_issue(1)["comments"] == []


def test_escalate_requires_a_question_and_options_considered(cfg, gh):
    do_escalate(
        cfg,
        gh,
        task=1,
        question="Which band applies?",
        considered="0.42-0.58 vs widening to 0.62",
        **BASE,
    )
    packet = parse_latest(gh.get_issue(1)["comments"], "handoff")
    assert packet["question"] == "Which band applies?"
    assert packet["considered"].startswith("0.42")
    assert "fleet:human" in gh.get_issue(1)["labels"]


def test_escalate_rejects_an_empty_question(cfg, gh):
    with pytest.raises(ValueError, match="question"):
        do_escalate(cfg, gh, task=1, question="   ", considered="something", **BASE)
    assert gh.get_issue(1)["comments"] == []


def test_escalate_rejects_missing_considered(cfg, gh):
    with pytest.raises(ValueError, match="considered"):
        do_escalate(cfg, gh, task=1, question="Which band?", considered="", **BASE)
    assert gh.get_issue(1)["comments"] == []


def test_handoff_cannot_reach_blocked(cfg, gh):
    """A handoff to blocked carries no blocked_on, so nothing could unblock it."""
    with pytest.raises(ValueError, match="do_block"):
        do_handoff(cfg, gh, task=1, to="blocked", **BASE)
    assert gh.get_issue(1)["comments"] == []
    assert gh.get_issue(1)["labels"] == ["fleet:claimed", "area:eval"]


def test_handoff_cannot_reach_human(cfg, gh):
    """A handoff to human carries no question, so there is nothing to answer."""
    with pytest.raises(ValueError, match="do_escalate"):
        do_handoff(cfg, gh, task=1, to="human", **BASE)
    assert gh.get_issue(1)["comments"] == []
    assert gh.get_issue(1)["labels"] == ["fleet:claimed", "area:eval"]


def test_a_task_cannot_block_on_itself(cfg, gh):
    """Both lookups resolve and the schema is satisfied, so nothing downstream
    catches it: the unblock watcher waits for the very task it is blocking, and
    block is the non-escalating path, so no human is ever woken. A
    `--task 1 --on 1` typo used to strand the task until the stall timer."""
    with pytest.raises(ValueError, match="itself"):
        do_block(cfg, gh, task=1, on=1, **BASE)
    assert gh.get_issue(1)["comments"] == []
    assert gh.get_issue(1)["labels"] == ["fleet:claimed", "area:eval"]


def test_blocked_on_cannot_ride_on_a_packet_bound_elsewhere(cfg, gh):
    """The schema lists blocked_on as a free optional property, so a REVIEW
    packet carrying one validates -- a phantom dependency an unblock watcher
    will act on. do_handoff's `**kw` passed it straight through."""
    with pytest.raises(ValueError, match="blocked_on"):
        do_handoff(cfg, gh, task=1, to="review", blocked_on=2, **BASE)
    assert gh.get_issue(1)["comments"] == []


def test_a_question_cannot_ride_on_a_packet_bound_elsewhere(cfg, gh):
    with pytest.raises(ValueError, match="question"):
        do_handoff(cfg, gh, task=1, to="review", question="why?", **BASE)
    assert gh.get_issue(1)["comments"] == []
