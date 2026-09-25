from __future__ import annotations

import pytest

from fleet.expand import expand
from fleet.labels import static_vocabulary
from fleet.model import (
    Advance,
    Assign,
    Close,
    CloseIssue,
    Continue,
    Dispatch,
    Drain,
    Escalate,
    Rebase,
    Release,
    Retire,
    SetLabels,
    StartAgent,
    StopAgent,
    Unblock,
)
from test_decide_reap import POLICY


def labels_of(effects):
    return [e for e in effects if isinstance(e, SetLabels)]


# Every intent that writes a state label. `Drain` is excluded because it writes
# nothing; its own emptiness is asserted separately. Both properties below are
# the module's reason for existing, so they are checked against each branch
# rather than against `Unblock` alone -- with only `Unblock` covered, appending
# a duplicate SetLabels to the Dispatch or Release branch survived all 305 tests.
MUTATING = [
    Unblock(task=4),
    Dispatch(task=1, role="implementer", base="integration", attempt=1),
    Release(task=1, reason="stalled", attempt=2),
    Escalate(task=1, reason="attempts_exhausted"),
    Advance(task=1, to="review"),
]


@pytest.mark.parametrize("intent", MUTATING, ids=lambda i: type(i).__name__)
def test_a_state_transition_is_exactly_one_labels_effect(intent):
    """A relabel is two gh calls, and removing before adding lets a failure
    between them strand the task with no state label at all, invisible to every
    label query. One effect keeps the two halves from drifting apart."""
    assert len(labels_of(expand(intent, POLICY))) == 1


def test_unblock_moves_blocked_to_ready():
    move = labels_of(expand(Unblock(task=4), POLICY))[0]
    assert "fleet:ready" in move.add
    assert "fleet:blocked" in move.remove


@pytest.mark.parametrize("intent", MUTATING, ids=lambda i: type(i).__name__)
def test_a_transition_never_removes_the_label_it_adds(intent):
    """GhClient has no combined edit -- only add_labels and remove_labels
    (gh.py:24-25) -- so an executor makes two calls in some order. Catches
    `remove=all_state_labels()`: applied add-then-remove it strands the task
    with no state label at all, the exact defect SetLabels exists to prevent."""
    move = labels_of(expand(intent, POLICY))[0]
    assert set(move.add) & set(move.remove) == set()


def test_dispatch_claims_the_task_and_starts_the_agent():
    effects = expand(Dispatch(task=1, role="implementer", base="integration", attempt=1), POLICY)
    assert StartAgent(task=1, role="implementer", base="integration") in effects
    add = labels_of(effects)[0].add
    assert "fleet:claimed" in add
    assert "fleet:attempt-1" in add


def test_dispatch_writes_the_attempt_number_it_was_given():
    """Catches the hardcoded attempt_label(1), the second writer that disagreed
    with the Release that had already counted the attempt."""
    effects = expand(Dispatch(task=1, role="implementer", base="integration", attempt=2), POLICY)
    assert "fleet:attempt-2" in labels_of(effects)[0].add


def test_dispatch_names_no_assignee():
    """`fleet-1` is not a GitHub login: `gh issue edit --add-assignee fleet-1`
    resolves the user first and fails the whole edit. Nothing in M2 knows the
    identity the executor runs agents as, and GhClient has no assignee method,
    so ownership is written in M3 by the executor that does. Catches a
    fabricated owner going back in."""
    effects = expand(Dispatch(task=1, role="implementer", base="integration", attempt=1), POLICY)
    assert [e for e in effects if isinstance(e, Assign)] == []


def test_release_stops_the_agent_unassigns_and_writes_the_attempt():
    effects = expand(Release(task=1, reason="stalled", attempt=2), POLICY)
    assert StopAgent(task=1) in effects
    assert Assign(task=1, owner=None) in effects
    add = labels_of(effects)[0].add
    assert "fleet:ready" in add
    assert "fleet:attempt-2" in add


def test_release_never_emits_an_attempt_label_beyond_the_cap():
    """labels.static_vocabulary is sized from max_attempts, so a label past the
    cap is one gh has never heard of -- and gh refuses an unknown label and
    changes nothing at all. Drift releases are not capped, so this arrives."""
    effects = expand(Release(task=1, reason="no_agent", attempt=3), POLICY)
    add = labels_of(effects)[0].add
    assert "fleet:attempt-3" not in add
    assert "fleet:attempt-2" in add


def test_a_zero_attempt_raises_rather_than_being_rounded_up():
    """Catches a clamp on the low end, which is the silent-reset defect the
    counter used to have: a caller that forgot the number wrote attempt-1 over a
    task already on its second try."""
    with pytest.raises(ValueError, match="attempts start at 1"):
        expand(Release(task=1, reason="stalled", attempt=0), POLICY)


def test_rebase_lands_in_ready_with_its_counter_and_no_new_attempt():
    """The attempt label is the one already on the task. A rebase that bumped it
    would inflate fleet:attempt-N, which is exactly what the grader's `no task
    exceeds 2 attempts` hard fail reads -- a task could reach NEEDS_HUMAN for
    having conflicted twice."""
    effects = expand(Rebase(task=4, rebase=1), POLICY)
    labelling = [e for e in effects if isinstance(e, SetLabels)]
    assert len(labelling) == 1
    assert "fleet:ready" in labelling[0].add
    assert "fleet:rebase-1" in labelling[0].add
    assert not any(label.startswith("fleet:attempt-") for label in labelling[0].add)
    assert effects[0] == StopAgent(task=4)


def test_rebase_never_emits_a_counter_label_beyond_the_cap():
    """Clamped for the same reason `_attempt_label` is: labels.static_vocabulary
    is sized from `max_rebases`, so `fleet:rebase-(cap+1)` is a label gh has
    never heard of, and gh refuses an unknown label and changes nothing at all.
    decide() never emits a Rebase past the cap -- it escalates instead -- but
    expand() has no way to know that about its caller, and a rebase number
    handed straight to `labels.rebase_label` without clamping would raise
    ValueError past `max_rebases` labels in the static vocabulary."""
    effects = expand(Rebase(task=4, rebase=5), POLICY)
    add = [e for e in effects if isinstance(e, SetLabels)][0].add
    assert not any(label.startswith("fleet:rebase-") and label != "fleet:rebase-1" for label in add)
    assert "fleet:rebase-1" in add


def test_escalate_reaches_human_and_stops_the_agent():
    effects = expand(Escalate(task=1, reason="attempts_exhausted"), POLICY)
    assert StopAgent(task=1) in effects
    assert "fleet:human" in labels_of(effects)[0].add


def test_advance_moves_to_the_named_state_and_stops_the_finished_agent():
    """The agent that wrote the packet has exited. Catches leaving it in the
    executor's live set, where it keeps reporting a cumulative cost every tick
    and drains the fleet budget on a process that finished."""
    effects = expand(Advance(task=1, to="review"), POLICY)
    assert StopAgent(task=1) in effects
    assert "fleet:review" in labels_of(effects)[0].add


def test_advancing_to_done_closes_the_issue():
    """Closing on completion is what makes the UNBLOCK fix load-bearing: a
    closed issue is a satisfied dependency, and until something closes one that
    rule never fires."""
    effects = expand(Advance(task=5, to="done"), POLICY)
    assert CloseIssue(task=5) in effects
    assert effects.index(CloseIssue(task=5)) == len(effects) - 1


def test_advancing_to_review_does_not_close_the_issue():
    effects = expand(Advance(task=5, to="review"), POLICY)
    assert not any(isinstance(e, CloseIssue) for e in effects)


def test_advance_refuses_a_state_outside_the_vocabulary():
    """A packet's `to` reaches here from an issue comment, so it is not
    necessarily a state this repo's label set contains."""
    with pytest.raises(ValueError, match="unknown state"):
        expand(Advance(task=1, to="merged"), POLICY)


def test_retire_stops_the_agent_and_changes_no_state():
    """The agent wrote the state itself with `fleet handoff`. A SetLabels here
    would overwrite the handoff's target with whatever the allocator last saw --
    turning a task that reached REVIEW back into a CLAIMED one."""
    effects = expand(Retire(task=5), POLICY)
    assert effects == [StopAgent(task=5), Assign(task=5, owner=None)]
    assert not any(isinstance(e, SetLabels) for e in effects)


def test_continue_starts_an_agent_and_touches_no_label():
    """A SetLabels here relabels a REVIEW task to CLAIMED, which destroys the
    state the continuation was dispatched for and routes the next lookup back to
    the implementer."""
    effects = expand(Continue(task=5, role="reviewer", base="integration"), POLICY)
    assert effects == [StartAgent(task=5, role="reviewer", base="integration")]


def test_drain_produces_no_effects():
    """Drain is a decision not to dispatch. There is nothing to write."""
    assert expand(Drain(reason="budget"), POLICY) == []


def test_every_label_written_is_within_the_static_vocabulary():
    """gh resolves a label to an id before adding or removing it, so naming a
    label the repo has never had fails the whole edit. FakeGh mirrors that."""
    vocabulary = static_vocabulary(POLICY.max_attempts, POLICY.max_fix_passes)
    for intent in MUTATING:
        for effect in labels_of(expand(intent, POLICY)):
            assert set(effect.add) <= vocabulary
            assert set(effect.remove) <= vocabulary


def test_close_closes_the_issue_and_stops_the_agent():
    """Spec §6's close, on the path the real shell actually takes: the agent
    moved the label to `fleet:done` itself, so there is nothing to relabel and
    only the close is left. StopAgent for the same reason Advance carries one --
    an agent left in the executor's live set keeps reporting cost every tick."""
    assert expand(Close(task=5), POLICY) == [CloseIssue(task=5), StopAgent(task=5)]


def test_close_writes_no_labels_at_all():
    """The label is the one thing the agent got right, and this intent must not
    be able to express a relabel -- the same property `Retire` has. A SetLabels
    here would let a close rewrite a terminal state an agent chose."""
    assert not [e for e in expand(Close(task=5), POLICY) if isinstance(e, SetLabels)]


def test_close_closes_before_it_stops_so_a_failed_close_can_be_retried():
    """Order, not just membership, and the opposite order from Advance-to-done.

    `decide` emits Close only while the row is OPEN and its agent is live, so
    those two facts ARE the retry condition. Stopping first spends the agent on
    an attempt that then fails its close, and the row sits OPEN at `fleet:done`
    for the rest of the night with nothing left to re-arm it. The reverse
    failure -- close lands, stop fails -- is already covered: that is a CLOSED
    row with an agent, which `_reap`'s closed-row rule retires.
    """
    effects = expand(Close(task=5), POLICY)
    assert effects.index(CloseIssue(task=5)) < effects.index(StopAgent(task=5))
