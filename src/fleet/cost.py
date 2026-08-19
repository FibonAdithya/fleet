"""Transcript tokens -> dollars. Pure.

herdr reports no cost and Claude Code writes no dollar figure, but every agent in
a herdr snapshot carries `agent_session.value` -- the Claude session UUID -- and
the transcript is `~/.claude/projects/<slug>/<uuid>.jsonl`. Verified against the
captured snapshot: 10 of 10 agents resolved to a transcript.

No herdr, no network, no clock. The budget ceiling is the part of M3 most likely
to be quietly wrong, so it is the part that gets a pure function.
"""

from __future__ import annotations

import json
from pathlib import Path

# fleet.toml's [prices] tables are keyed by model and carry these four rates, in
# dollars per million tokens. The mapping to Claude Code's counter names lives
# here and nowhere else.
_COUNTERS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}

PRICE_KEYS = tuple(_COUNTERS)

_PER = 1_000_000


class UnpricedModel(Exception):
    """A transcript names a model the price table does not.

    Raised rather than defaulted to zero: a zero price switches the ceiling off
    silently, and the event that causes it -- a model changing under an agent --
    happens without anyone editing fleet.toml. The daemon's response is to drain,
    which is what parent §10 already prescribes for a budget it cannot honour.
    """


def price_lines(lines: list[str], prices: dict[str, dict[str, float]]) -> float:
    total = 0.0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            # A transcript being appended to while we read it ends mid-line.
            # Skipping the partial line costs one message of accuracy for one
            # tick; crashing costs the whole tick.
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") or {}
        usage = message.get("usage")
        if not usage:
            continue
        model = message.get("model")
        table = prices.get(model)
        if table is None:
            raise UnpricedModel(
                f"no price for model {model!r}. Add it to [prices] in fleet.toml. "
                "Pricing it at zero would disable the budget ceiling instead."
            )
        for key, field in _COUNTERS.items():
            total += (usage.get(field) or 0) * table[key] / _PER
    return total


def price_transcript(path: Path, prices: dict[str, dict[str, float]]) -> float:
    return price_lines(Path(path).read_text(encoding="utf-8").splitlines(), prices)


def transcript_path(session_id: str, projects_dir: Path) -> Path | None:
    """`~/.claude/projects/*/<uuid>.jsonl`, or None.

    None when herdr knows about an agent whose transcript has not been written
    yet -- ordinary in the seconds after a dispatch. A fact about the filesystem,
    not an error: raising here would take down a tick over a race.
    """
    hits = sorted(Path(projects_dir).glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None
