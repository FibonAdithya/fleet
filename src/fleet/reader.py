"""The daemon's ledger read: rows, plus packets for the rows that carry one.

Split out of the CLI's `do_plan` because it needs two things `do_plan` does not:
the `all` query, and comment bodies. Both are M3-only, and both are things
m2-inputs §7 names as inherited debt.
"""

from __future__ import annotations

from collections.abc import Collection

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


def read_ledger(gh: GhClient, areas: Collection[str] = labels_module.AREAS) -> list[Task]:
    """Every row, open and closed, with packets for the states that carry one.

    `all`, not `open`. Once packets are fetched, `blocked_on` is populated and
    UNBLOCK becomes reachable -- and an open-only query drops the closed
    dependency that would satisfy it, reintroducing the same wedge from the
    query side that the `_open` split removes from the filter side.

    Closed rows are returned rather than filtered: three phases in `decide()`
    disagree about what a closed row means, and filtering here would take that
    choice away from the only code that can make it.
    """
    rows = gh.list_issues("all")
    packets: dict[int, dict] = {}
    for row in rows:
        shallow = ledger_module.read_one(row, areas=areas)
        if shallow.state not in PACKET_STATES:
            continue
        found = packet_module.parse_latest(
            gh.get_issue(row["number"]).get("comments") or [], "handoff"
        )
        if found is not None:
            packets[row["number"]] = found
    return ledger_module.read(rows, packets, areas=areas)
