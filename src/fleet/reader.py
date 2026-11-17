"""The daemon's ledger read: rows, plus packets for the rows that carry one.

Split out of the CLI's `do_plan` because it needs two things `do_plan` does not:
the `all` query, and comment bodies. Both are M3-only, and both are things
m2-inputs §7 names as inherited debt.
"""

from __future__ import annotations

from collections.abc import Collection

from fleet import escalation as escalation_module
from fleet import labels as labels_module
from fleet import ledger as ledger_module
from fleet import packet as packet_module
from fleet.gh import GhClient
from fleet.model import Task

# The states whose rows carry a packet worth fetching.
#
# `blocked` is here because `Task.blocked_on` comes from the packet and nothing
# else -- without it UNBLOCK has no input at all. `ready` and `done` are not:
# nothing reads a packet in either state, and `gh issue view` is one API call per
# issue, so fetching for every row turns one list call into fifty.
#
# `claimed`, `review` and `integrating` are `decide._LIVE` -- every state
# `decide._reap` walks with `_is_rebase_handback`, which reads `task.packet`
# (via `packet or {}`, so a missing one just reads as "not a handback") for any
# row in one of the three before it branches on which one. `claimed` is also
# read directly, by `decide._advance`, to route a finished implementer's
# handoff. A row at `review` never legitimately carries a rebase handback --
# only an integrator, working at `integrating`, writes one -- so its packet
# never changes `_reap`'s answer; it is fetched to match the call site exactly
# rather than to satisfy a decision that depends on it.
PACKET_STATES = ("claimed", "review", "integrating", "blocked")


def read_ledger(
    gh: GhClient, areas: Collection[str] = labels_module.AREAS, login: str | None = None
) -> list[Task]:
    """Every row, open and closed, with packets for the states that carry one.

    `all`, not `open`. Once packets are fetched, `blocked_on` is populated and
    UNBLOCK becomes reachable -- and an open-only query drops the closed
    dependency that would satisfy it, reintroducing the same wedge from the
    query side that the `_open` split removes from the filter side.

    Closed rows are returned rather than filtered: three phases in `decide()`
    disagree about what a closed row means, and filtering here would take that
    choice away from the only code that can make it.

    `login` is the account the daemon runs as; it marks the owner's own issues
    (`Task.by_owner`), which is what ADOPT requires. None marks nothing.
    """
    rows = gh.list_issues("all")
    packets: dict[int, dict] = {}
    for row in rows:
        shallow = ledger_module.read_one(row, areas=areas)
        if shallow.state not in PACKET_STATES:
            continue
        comments = gh.get_issue(row["number"]).get("comments") or []
        found = current_packet(comments, answers=shallow.answers)
        if found is not None:
            packets[row["number"]] = found
    return ledger_module.read(rows, packets, areas=areas, login=login)


def current_packet(comments: list[str], answers: int = 0) -> dict | None:
    """The newest handoff packet, unless the escalation it belongs to has been
    dealt with since it was written.

    The issue outlives the attempt, and after an owner's answer `_dispatch`
    stamps `max(task.attempts, 1)` again, so the attempt binding in
    `decide._advance` and `decide._is_rebase_handback` no longer tells the
    escalated attempt's packet from the new agent's. Two shapes, both real:
    the newest packet IS the escalation (a handoff to `human`), which then
    advances a finished agent's task straight back to `fleet:human` with no new
    escalation comment, so the owner is never pinged; or the allocator
    escalated with a marker comment after a same-attempt `to: review` packet or
    an integrator's rebase handback, which then advances or rebases on the work
    the owner asked to change.

    So the packet is stale when, after it:
    - the allocator escalated (a marker comment). The packet is then evidence
      from before the escalation, whether or not the owner has answered yet.
    - the owner answered (`escalation.is_answer`). Counted only when the issue
      carries an answer label (`answers` is the highest `fleet:answered-<n>`):
      anyone can post a comment that starts like an answer, but hermes-harness
      adds the label straight after its comment, and only a collaborator can.

    A packet to `human` with neither after it is current: it is the agent's own
    escalation, unanswered, and a finished agent's task advances to
    `fleet:human` with its question even if the agent's own label move failed
    (R19). Staleness is decided against the answer, not the escalation.

    One fallback for an answer fleet cannot place: if the issue carries more
    answer labels than answer comments (a label added by hand, or a changed
    hermes-harness comment), a packet to `human` is treated as answered.
    A missed answer would send the task back to `fleet:human` with no ping,
    and a missed R19 only costs an attempt, so this errs toward stale.
    """
    index = packet_module.latest_index(comments, "handoff")
    if index is None:
        return None
    found = packet_module.parse_latest(comments, "handoff")
    later = comments[index + 1 :]
    if any(escalation_module.is_marker(body) for body in later):
        return None
    if answers >= 1 and any(escalation_module.is_answer(body) for body in later):
        return None
    placed = sum(1 for body in comments if escalation_module.is_answer(body))
    if found is not None and found.get("to") == "human" and answers > placed:
        return None
    return found
