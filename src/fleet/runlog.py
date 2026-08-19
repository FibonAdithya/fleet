"""Append-only JSONL run log.

Harness §7 makes this one of the three inputs `fleet grade` needs, alongside the
final ledger and `git log integration`. `simulate.run` writes one for every tick
it replays; nothing in M2 reads it back except these tests and the grader that
is coming.

It is also the only place a transient label survives. m2-inputs §3 notes that
rubric 04 asserts fleet:blocked was applied, and the next transition removes it
-- so the final ledger cannot answer the question and this file can.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path

from fleet.model import Intent


def _as_dict(intent: Intent) -> dict:
    """Every field, plus the class name. No intent in M2 has a field called
    `kind`; one added later would be silently overwritten by this splat."""
    return {"kind": type(intent).__name__, **dataclasses.asdict(intent)}


class RunLog:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self, *, tick: int, at: datetime, intent: Intent, outcome: str, cost_usd: float = 0.0
    ) -> None:
        entry = {
            "run_id": self.run_id,
            "tick": tick,
            "at": at.isoformat(),
            "intent": _as_dict(intent),
            "outcome": outcome,
            # Only a reaped agent's cost is final, so only a reaping intent
            # carries a non-zero figure. This is what a restarted daemon sums
            # to recover spent_usd: without it the fleet forgets the night's
            # spend and the ceiling stops binding on the one path -- restart
            # -- the design depends on working. Always written, even when
            # zero: 0.0 is a real cost (a free tick), not an absence, and it
            # must be distinguishable from an entry that predates this field.
            "cost_usd": cost_usd,
        }
        # Append mode, one line per entry: a run that dies mid-night leaves
        # everything up to that point intact and parseable.
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def read(path: Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines() if line]


def spent_from_log(path: Path, run_id: str | None = None) -> float:
    """Rebuild `FleetState.spent_usd` from the log.

    Derived and disposable, still -- but reconstructible, which is what
    parent §9's "holds no authoritative state" always meant. A missing file
    is zero: a fresh fleet has not written one yet, and raising would stop
    the daemon starting.

    Tolerant of a truncated final line, because a daemon killed mid-write is
    precisely the event this exists to recover from: the line is skipped,
    not counted, and nothing after it is expected to exist either.

    Tolerant, too, of an entry with no "cost_usd" key at all -- the shape
    every entry written before this change has. That contributes 0.0, same
    as a missing file: there is no way to recover what an entry actually
    cost when the log never recorded it, and refusing to start would break
    the one restart path this function exists to serve. This is a known,
    silent underestimate of true historical spend for logs old enough to
    predate cost tracking -- not a claim that those entries were free.
    """
    path = Path(path)
    if not path.is_file():
        return 0.0
    total = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if run_id is not None and entry.get("run_id") != run_id:
            continue
        total += float(entry.get("cost_usd", 0.0))
    return total


def latest_run_id(path: Path) -> str | None:
    """The run id of the last entry in the log, or None for an empty log.

    What a restart rejoins. `spent_from_log` filters on a run id, so a daemon
    that mints a fresh one at every start filters on an id appearing in zero
    lines and resumes at `spent_usd = 0.0` -- spec §8's failure, exactly, and
    invisible because the number it produces is a legal one.

    The LAST entry, not the first: one file holds several nights, and the night
    a restart means to rejoin is the one that was being written when the
    process died.

    Tolerant of the same two shapes `spent_from_log` is, and for the same
    reason: a truncated final line is what a `kill -9` mid-write leaves, and an
    entry with no usable `run_id` is skipped rather than allowed to answer.
    Returning None on an unreadable log is safe in the direction that matters
    -- the caller mints a fresh id and starts at zero, which under-spends
    rather than over-spends only if the log was already unreadable, in which
    case there was nothing to recover.
    """
    path = Path(path)
    if not path.is_file():
        return None
    found: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        run_id = entry.get("run_id")
        if isinstance(run_id, str) and run_id:
            found = run_id
    return found
