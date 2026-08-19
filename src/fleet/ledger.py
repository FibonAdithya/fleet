"""Raw gh JSON -> Task. The one place that absorbs fake/real divergence.

Four of M0-M1's defects were cases where FakeGh was subtly more forgiving than
RealGh, each of which passed the entire offline suite and would have failed
against GitHub (docs/m2-inputs.md §6). Concentrating the parse means parity is
one module's standing obligation rather than a property every caller has to
remember.
"""

from __future__ import annotations

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


def _one_of(names: list[str], prefix: str, allowed: tuple[str, ...], kind: str) -> str | None:
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


def read_one(row: dict, packet: dict | None = None, verdict: dict | None = None) -> Task:
    names = _names(row.get("labels", []))
    assignees = row.get("assignees") or []
    first = assignees[0] if assignees else None
    return Task(
        number=row["number"],
        title=row.get("title", ""),
        # No state label reads as triage rather than raising: _post removes all
        # eight then adds one, so a crash between the two is a real state the
        # allocator has to be able to see.
        state=_state(names) or "triage",
        area=_one_of(names, "area:", labels_module.AREAS, "area"),
        klass=_one_of(names, "class:", labels_module.CLASSES, "class"),
        auto_ok=labels_module.AUTO_OK in names,
        attempts=_counter(names, "fleet:attempt-"),
        fix_passes=_counter(names, "fleet:fix-pass-"),
        rebases=_counter(names, "fleet:rebase-"),
        # gh's assignees are objects; FakeGh has no assignees at all and callers
        # that build rows by hand use bare logins. Accept all three.
        assignee=first.get("login") if isinstance(first, dict) else first,
        # gh returns OPEN/CLOSED; matched case-insensitively for the same reason
        # FakeGh.list_issues does it -- gh's --state flag accepts any casing.
        is_open=str(row.get("state", "OPEN")).upper() != "CLOSED",
        blocked_on=(packet or {}).get("blocked_on"),
        packet=packet,
        verdict=verdict,
    )


def read(
    rows: list[dict],
    packets: dict[int, dict] | None = None,
    verdicts: dict[int, dict] | None = None,
) -> list[Task]:
    packets = packets or {}
    verdicts = verdicts or {}
    return [read_one(row, packets.get(row["number"]), verdicts.get(row["number"])) for row in rows]
