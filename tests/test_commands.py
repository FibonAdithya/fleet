from __future__ import annotations

import pytest

from fleet.commands import do_block, do_escalate, do_handoff, do_verdict
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


VERDICT = dict(role="reviewer", reviewer="fleet-4", head_sha="a91c204")


@pytest.fixture()
def in_review(gh):
    """Issue 1 in REVIEW.

    The `gh` fixture leaves it CLAIMED, which is exactly where a verdict has to
    be refused -- so the move is made here rather than folded into `gh`, where it
    would quietly satisfy the state gate for every test in the file.
    """
    gh.remove_labels(1, ["fleet:claimed"])
    gh.add_labels(1, ["fleet:review"])
    return gh


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


def test_an_approved_verdict_moves_the_task_to_integrating(cfg, in_review):
    """Catches a target table that leaves an approved task in REVIEW, and a
    _move_state that adds the new label without removing the old one -- two
    state labels on one row is what ledger.read_one refuses."""
    _, landed = do_verdict(
        cfg,
        in_review,
        task=1,
        verdict="approved",
        findings=[],
        checks_rerun="make check: pass",
        **VERDICT,
    )
    names = in_review.get_issue(1)["labels"]
    assert landed == "integrating"
    assert "fleet:integrating" in names
    assert "fleet:review" not in names
    assert "area:eval" in names


def test_changes_required_spends_a_fix_pass_on_the_way_back_to_ready(cfg, in_review):
    """Catches two mutations. A target that writes no counter: decide() reads fix
    passes off `fleet:fix-pass-N`, so a round that writes none is a round the
    allocator cannot see and the cap never binds. And a target of `claimed`
    instead of `ready`: REAP releases a CLAIMED task with no live agent as
    drift, which spends an attempt on a fix pass that ruling 2 says is free."""
    _, landed = do_verdict(
        cfg,
        in_review,
        task=1,
        verdict="changes_required",
        findings=[{"severity": "must_fix", "detail": "no test binds the fix"}],
        **VERDICT,
    )
    names = in_review.get_issue(1)["labels"]
    assert landed == "ready"
    assert "fleet:ready" in names
    assert "fleet:claimed" not in names
    assert "fleet:fix-pass-1" in names


def test_a_second_changes_required_reaches_a_human_rather_than_looping(cfg, in_review):
    """Spec §5 ruling 4's other half. With the cap spent another round back to
    READY is a second fix pass on a one-pass budget, and nothing else in the
    system stops it. Catches a target table that ignores max_fix_passes."""
    in_review.add_labels(1, ["fleet:fix-pass-1"])
    _, landed = do_verdict(
        cfg,
        in_review,
        task=1,
        verdict="changes_required",
        findings=[{"severity": "must_fix", "detail": "still no test"}],
        **VERDICT,
    )
    names = in_review.get_issue(1)["labels"]
    assert landed == "human"
    assert "fleet:human" in names
    assert "fleet:ready" not in names


def test_the_verdict_round_trips_through_the_comment(cfg, in_review):
    """Catches a render/parse pair that agrees with itself but not with the
    marker and schema id parse_latest keys on -- the shell reads the verdict back
    out of the comment, so a payload it cannot recover is a verdict decide()
    never sees."""
    do_verdict(cfg, in_review, task=1, verdict="approved", findings=[], **VERDICT)
    recovered = parse_latest(in_review.get_issue(1)["comments"], "verdict")
    assert recovered["verdict"] == "approved"
    assert recovered["head_sha"] == "a91c204"


def test_a_verdict_without_a_head_sha_is_refused(cfg, in_review):
    """A verdict that does not name the head it reviewed is not a gate. Catches
    validating after the label move, which strands the task in INTEGRATING with
    no verdict to justify it."""
    with pytest.raises(SchemaError, match="head_sha"):
        do_verdict(
            cfg, in_review, task=1, verdict="approved", findings=[], **{**VERDICT, "head_sha": ""}
        )
    assert in_review.get_issue(1)["comments"] == []
    assert "fleet:review" in in_review.get_issue(1)["labels"]


def test_changes_required_needs_at_least_one_finding(cfg, in_review):
    """The schema allows an empty findings array because an approval has nothing
    to list. Catches dropping the extra guard: "changes required" with nothing to
    change is a task the implementer cannot action, and it spends the fix pass."""
    with pytest.raises(ValueError, match="at least one finding"):
        do_verdict(cfg, in_review, task=1, verdict="changes_required", findings=[], **VERDICT)
    assert in_review.get_issue(1)["comments"] == []
    assert "fleet:fix-pass-1" not in in_review.get_issue(1)["labels"]


def test_an_unknown_verdict_value_is_refused(cfg, in_review):
    """Catches computing the target state before validating, which would map an
    unrecognised verdict onto whichever branch the code happens to fall through
    to."""
    with pytest.raises(SchemaError, match="verdict"):
        do_verdict(cfg, in_review, task=1, verdict="looks good to me", findings=[], **VERDICT)
    assert in_review.get_issue(1)["comments"] == []


def test_an_implementer_cannot_approve_its_own_task(cfg, in_review):
    """The hole this command would otherwise open: an implementer approving its
    own work reaches INTEGRATING with nobody having read the diff, and grading
    reads terminal state from labels. Catches dropping the role gate."""
    with pytest.raises(ValueError, match="implementer.*may not write a verdict"):
        do_verdict(
            cfg,
            in_review,
            task=1,
            verdict="approved",
            findings=[],
            **{**VERDICT, "role": "implementer"},
        )
    assert in_review.get_issue(1)["comments"] == []
    assert "fleet:integrating" not in in_review.get_issue(1)["labels"]


def test_the_invariant_reviewer_may_write_one_too(cfg, in_review):
    """invariant-reviewer.md ends "Emit `approved` only if all five are `ok`".
    Catches narrowing VERDICT_ROLES to {"reviewer"}, which leaves that role with
    no way to record the verdict its own prompt demands."""
    _, landed = do_verdict(
        cfg,
        in_review,
        task=1,
        verdict="approved",
        findings=[],
        **{**VERDICT, "role": "invariant-reviewer"},
    )
    assert landed == "integrating"


def test_a_verdict_on_a_task_that_never_reached_review_is_refused(cfg, gh):
    """The `gh` fixture leaves issue 1 CLAIMED. Catches dropping the state gate:
    a verdict would then walk a task nobody has reviewed straight into
    INTEGRATING, which is the hole the role gate closes from the other side."""
    with pytest.raises(ValueError, match="not review"):
        do_verdict(cfg, gh, task=1, verdict="approved", findings=[], **VERDICT)
    assert gh.get_issue(1)["comments"] == []
    assert gh.get_issue(1)["labels"] == ["fleet:claimed", "area:eval"]
