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

    def append(self, *, tick: int, at: datetime, intent: Intent, outcome: str) -> None:
        entry = {
            "run_id": self.run_id,
            "tick": tick,
            "at": at.isoformat(),
            "intent": _as_dict(intent),
            "outcome": outcome,
        }
        # Append mode, one line per entry: a run that dies mid-night leaves
        # everything up to that point intact and parseable.
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def read(path: Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines() if line]
