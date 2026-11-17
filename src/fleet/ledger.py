"""Raw gh JSON -> Task. The one place that absorbs fake/real divergence.

Four of M0-M1's defects were cases where FakeGh was subtly more forgiving than
RealGh, each of which passed the entire offline suite and would have failed
against GitHub (docs/m2-inputs.md §6). Concentrating the parse means parity is
one module's standing obligation rather than a property every caller has to
remember.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from fleet import labels as labels_module
from fleet.model import Task


class LedgerError(Exception):
    """A ledger row could not be understood."""


def _names(raw: list) -> list[str]:
    """gh returns label objects; FakeGh stores names. Accept both."""
    out = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name")
            if not name:
                raise LedgerError(f"label object with no name: {item!r}")
            out.append(name)
        else:
            out.append(item)
    return out


def _state(names: list[str]) -> str | None:
    """The row's fleet state, or None when it carries none.

    A membership test against the eight state labels, NOT a `fleet:` prefix test.
    `fleet:auto-ok`, `fleet:attempt-N` and `fleet:fix-pass-N` share that prefix
    without being states, so a prefix test rejects 11 of the 13 rows in
    fixtures/ledger/issues.json -- every real task that is dispatchable or has
    been tried once.
    """
    found = sorted(set(names) & set(labels_module.all_state_labels()))
    if len(found) > 1:
        raise LedgerError(f"more than one state label: {found}")
    return found[0].split(":", 1)[1] if found else None


def _counter(names: list[str], prefix: str) -> int:
    """The highest N present. Labels accumulate -- attempt-1 is not removed when
    attempt-2 is added -- so the maximum is the count, not the length."""
    found = []
    for name in names:
        if name.startswith(prefix):
            suffix = name[len(prefix) :]
            if suffix.isdigit():
                found.append(int(suffix))
    return max(found, default=0)


def _one_of(names: list[str], prefix: str, allowed: Collection[str], kind: str) -> str | None:
    """The single value carried under `prefix`, validated against `allowed`.

    Two areas is as broken as an unknown one: area exclusion holds ONE area per
    task, so a task labelled area:eval and area:data would be excluded from one
    while an agent edits both. Unknown is checked first so the error names the
    typo rather than the count.
    """
    values = sorted({n[len(prefix) :] for n in names if n.startswith(prefix)})
    for value in values:
        if value not in allowed:
            raise LedgerError(f"unknown {kind}: {value!r}")
    if len(values) > 1:
        raise LedgerError(f"more than one {kind}: {values}")
    return values[0] if values else None


#: The label the box puts on an issue its agent filed. Not fleet's: it is
#: outside `labels.static_vocabulary`, and nothing in fleet writes it.
AGENT_REPORTED = "agent-reported"


def _login(raw: object) -> str | None:
    """gh's `author` is an object; RealGh and FakeGh hand back the bare login.
    Accept both, and None for a row that never carried the field."""
    if isinstance(raw, dict):
        return raw.get("login")
    return raw if isinstance(raw, str) else None


def _created_at(raw: object) -> datetime | None:
    """gh's ISO `createdAt` as an aware datetime. Parsed here so `decide` stays
    a comparison of datetimes. Missing reads as None (no age, never adopted);
    present but unreadable or without an offset is refused, because a naive
    time compared with an aware `now` raises inside decide."""
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise LedgerError(f"createdAt is not an ISO time with an offset: {raw!r}")
    return parsed


def read_one(
    row: dict,
    packet: dict | None = None,
    verdict: dict | None = None,
    areas: Collection[str] = labels_module.AREAS,
    login: str | None = None,
) -> Task:
    """`login` is the account the daemon runs as. A row is `by_owner` only when
    its author is that login; with no login, no row is."""
    names = _names(row.get("labels", []))
    assignees = row.get("assignees") or []
    first = assignees[0] if assignees else None
    state = _state(names)
    author = _login(row.get("author"))
    return Task(
        number=row["number"],
        title=row.get("title", ""),
        # No state label reads as triage rather than raising. _post now adds
        # before it removes, so it can no longer produce this, but a human edit
        # or an older CLI can: project-b#22 was left label-less that way on
        # 2026-09-25, and the allocator has to be able to see such a row.
        state=state or "triage",
        area=_one_of(names, "area:", areas, "area"),
        klass=_one_of(names, "class:", labels_module.CLASSES, "class"),
        auto_ok=labels_module.AUTO_OK in names,
        attempts=_counter(names, "fleet:attempt-"),
        fix_passes=_counter(names, "fleet:fix-pass-"),
        rebases=_counter(names, "fleet:rebase-"),
        answers=_counter(names, "fleet:answered-"),
        # gh's assignees are objects; FakeGh has no assignees at all and callers
        # that build rows by hand use bare logins. Accept all three.
        assignee=first.get("login") if isinstance(first, dict) else first,
        # gh returns OPEN/CLOSED; matched case-insensitively for the same reason
        # FakeGh.list_issues does it -- gh's --state flag accepts any casing.
        is_open=str(row.get("state", "OPEN")).upper() != "CLOSED",
        blocked_on=(packet or {}).get("blocked_on"),
        packet=packet,
        verdict=verdict,
        agent_reported=AGENT_REPORTED in names,
        by_owner=login is not None and author == login,
        created_at=_created_at(row.get("createdAt")),
        has_state_label=state is not None,
    )


def read(
    rows: list[dict],
    packets: dict[int, dict] | None = None,
    verdicts: dict[int, dict] | None = None,
    areas: Collection[str] = labels_module.AREAS,
    login: str | None = None,
) -> list[Task]:
    packets = packets or {}
    verdicts = verdicts or {}
    return [
        read_one(row, packets.get(row["number"]), verdicts.get(row["number"]), areas, login)
        for row in rows
    ]
