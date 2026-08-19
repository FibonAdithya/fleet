"""Intent -> effects. Pure.

Kept separate from decide() so that decide()'s tests read as policy -- they
assert Dispatch(5, implementer, 1), not a list of label strings -- while the
mutation sequence still gets covered. That sequence needs the coverage:
commands._post removes all eight state labels and then adds one, and a failure
between the two leaves a task with no state label at all.
"""

from __future__ import annotations

from fleet import labels as labels_module
from fleet.model import (
    Advance,
    Assign,
    Close,
    CloseIssue,
    Continue,
    Dispatch,
    Drain,
    Effect,
    Escalate,
    Intent,
    Policy,
    Rebase,
    Release,
    Retire,
    SetLabels,
    StartAgent,
    StopAgent,
    Unblock,
)


def _to_state(task: int, state: str, extra: tuple[str, ...] = ()) -> SetLabels:
    """One effect carrying both halves of the move.

    `GhClient` has no combined edit -- only `add_labels` and `remove_labels`
    (gh.py:24-25) -- so an executor makes two calls in some order. The label
    being added is therefore excluded from `remove`: leaving it in strands the
    task with no state label at all under add-then-remove, which is the exact
    defect this effect exists to prevent.
    """
    add = (labels_module.state_label(state), *extra)
    return SetLabels(
        task=task,
        add=add,
        remove=tuple(label for label in labels_module.all_state_labels() if label not in add),
    )


def _attempt_label(attempt: int, policy: Policy) -> str:
    """Clamped at max_attempts. labels.static_vocabulary is sized from that
    number, so attempt-(cap+1) is a label gh has never heard of -- and gh
    refuses an unknown label by failing the whole edit and changing nothing.

    Clamped at the top only. An attempt of 0 raises out of labels.attempt_label,
    because a zero attempt is the silent-reset defect this counter used to have,
    not a number to round up."""
    return labels_module.attempt_label(min(attempt, policy.max_attempts))


def expand(intent: Intent, policy: Policy) -> list[Effect]:
    if isinstance(intent, Drain):
        return []

    if isinstance(intent, Dispatch):
        # No Assign: `fleet-1` is not a GitHub login, and this layer has no way
        # to learn the one `gh` is authenticated as. HerdrExecutor writes
        # ownership when it starts the agent -- see herdr_executor.apply.
        return [
            _to_state(intent.task, "claimed", (_attempt_label(intent.attempt, policy),)),
            StartAgent(task=intent.task, role=intent.role, base=intent.base),
        ]

    if isinstance(intent, Continue):
        # StartAgent alone. The state label is already what this stage needs --
        # the previous stage's agent wrote it -- and no counter moves, because
        # nothing failed.
        return [StartAgent(task=intent.task, role=intent.role, base=intent.base)]

    if isinstance(intent, Release):
        return [
            StopAgent(task=intent.task),
            Assign(task=intent.task, owner=None),
            _to_state(intent.task, "ready", (_attempt_label(intent.attempt, policy),)),
        ]

    if isinstance(intent, Rebase):
        # No attempt label: the task keeps the one it already carries. Labels
        # accumulate and `_to_state` removes only state labels, so writing none
        # leaves the existing counter exactly where it was.
        return [
            StopAgent(task=intent.task),
            Assign(task=intent.task, owner=None),
            _to_state(
                intent.task,
                "ready",
                (labels_module.rebase_label(min(intent.rebase, policy.max_rebases)),),
            ),
        ]

    if isinstance(intent, Escalate):
        return [
            StopAgent(task=intent.task),
            Assign(task=intent.task, owner=None),
            _to_state(intent.task, "human"),
        ]

    if isinstance(intent, Unblock):
        return [_to_state(intent.task, "ready")]

    if isinstance(intent, Retire):
        # StopAgent for the same reason Advance carries one: an agent left in the
        # executor's live set keeps reporting cost every tick, and the budget
        # drain is computed from live agents. Assign(None) because the allocator
        # is the sole writer of ownership and this task no longer has an owner.
        return [StopAgent(task=intent.task), Assign(task=intent.task, owner=None)]

    if isinstance(intent, Close):
        # No SetLabels, on purpose. The row is already at `fleet:done` because
        # the agent's own `fleet handoff --to done` put it there, and this
        # intent must not be able to rewrite a terminal label an agent chose --
        # the same property Retire has, kept by having no state to write.
        #
        # Close FIRST, stop second -- the opposite of Advance-to-done, and for a
        # reason that only holds here. Advance writes a label, so its close goes
        # last: an edit failing after the close would leave a closed issue with
        # no terminal state. This intent writes no label, and what it needs
        # instead is to be retryable.
        #
        # `decide` emits this only while the row is OPEN and its agent is live,
        # so a close that fails leaves both facts true and the next tick tries
        # again. Stopping first would spend the agent -- the only evidence the
        # fleet ran this task -- on an attempt that then failed, and the row
        # would sit OPEN at `fleet:done` for the rest of the night with nothing
        # left to re-arm the retry.
        #
        # The reverse failure is already handled: a close that lands over a stop
        # that fails leaves a CLOSED row with an agent, which is `_reap`'s
        # closed-row rule, which retires it.
        return [CloseIssue(task=intent.task), StopAgent(task=intent.task)]

    if isinstance(intent, Advance):
        if intent.to not in labels_module.STATES:
            # A packet's `to` arrives from an issue comment, so it is not
            # necessarily a state this repo's label set contains.
            raise ValueError(f"unknown state in packet: {intent.to!r}")
        # StopAgent because the agent that wrote the packet has exited. Left in
        # the executor's live set it keeps reporting cost every tick, and the
        # budget drain is computed from live agents.
        effects: list[Effect] = [StopAgent(task=intent.task), _to_state(intent.task, intent.to)]
        if intent.to == "done":
            # Last, not first: the label is what a grader reads, and an edit
            # that fails after the close would leave a closed issue with no
            # terminal state at all.
            effects.append(CloseIssue(task=intent.task))
        return effects

    raise ValueError(f"cannot expand {intent!r}")
