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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
    """Delegates to `_claude_cost` over one `_decode_lines` pass.

    Used to carry its own decode loop; that copy and `_claude_cost` were
    compared over 10 cases (blank/malformed lines, non-dict entries,
    missing/empty/null usage, all four counters, zero usage, `<synthetic>`,
    multi-message) with zero divergences, so the loop was retired in favour of
    this delegation rather than kept as a second copy for `ClaudeProvider.read`
    to diverge from silently.
    """
    return _claude_cost(list(_decode_lines(lines)), prices)


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


Prices = dict[str, dict[str, float]]


@dataclass(frozen=True)
class Reading:
    """One transcript read: what it cost, and which models actually ran.

    Both facts come from one parse because the declared-vs-observed check needs
    the model set and the ceiling needs the cost, and re-reading every
    transcript twice per agent per tick is waste the daemon pays every minute.

    `models` is the set of models that carried NONZERO tokens. That definition
    excludes `<synthetic>` -- whose usage block is all zeros -- for free, so the
    pseudo-model needs no special case here or in `decide`.
    """

    cost_usd: float
    models: frozenset[str]


@dataclass(frozen=True)
class Session:
    """The transcripts one agent session wrote: its own, and its subagents'.

    A pair rather than a flat set of paths, because the two halves are read
    differently and issue #5's naive "return every path" would have lost that.
    Cost sums over `parent` AND `children` -- subagent spend is spend. The
    OBSERVED MODEL comes from `parent` alone: measured over the 66 local
    sessions that have both, 43 (65%) run a model in a child that the parent
    never ran, so folding children into the model set would hand
    `MULTIPLE_MODELS` to `decide._model_mismatch` and escalate two thirds of
    every task that spawns a subagent -- on a check that exists to catch a
    substitution in the agent the POLICY DECLARED, which is the parent.

    `children` is empty for a provider that cannot resolve them, which is not
    the same as a session that has none. `CodexProvider` is the live case: it
    refuses such a session in `read` rather than reporting it childless here.
    """

    parent: Path
    children: tuple[Path, ...] = ()


class Provider(Protocol):
    """One agent CLI's transcript format.

    `read` is pure: a function of lines and rates, nothing else. Only
    `transcript` touches the filesystem. That split is the same one the module
    docstring already defends -- the ceiling is the part most likely to be
    quietly wrong, so it is the part that stays a pure function.
    """

    name: str

    def session(self, session_id: str) -> Session | None: ...

    def read(self, lines: list[str], prices: Prices) -> Reading: ...


def _decode_lines(lines: list[str]) -> Iterator[dict]:
    """Strip, parse, and filter raw transcript lines to decoded dict records.

    Shared by every provider's `read` so the cost and the model set come from
    one JSON-decode pass over `lines`, not two independent scans of the same
    file. A transcript being appended to while we read it ends mid-line;
    skipping that partial line on `ValueError` costs one message of accuracy
    for one tick, crashing costs the whole tick -- the same trade `price_lines`
    makes, preserved here rather than re-decided.
    """
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        yield entry


def _carried_tokens(usage: dict) -> bool:
    """Whether a usage block bills anything at all.

    One predicate, two readers, on purpose. `_claude_models` uses it to decide
    which models were OBSERVED and `_claude_cost` to decide which records need
    a rate; two copies of "carried tokens" that drifted apart would make the
    cost path refuse a transcript the model path had already agreed to ignore.
    """
    return any((usage.get(field) or 0) for field in _COUNTERS.values())


def _claude_cost(entries: list[dict], prices: Prices) -> float:
    """The pricing rule, over entries `read` (or `price_lines`) has decoded.

    The single copy of the loop `price_lines` used to carry directly: kept as
    entries-in rather than lines-in so `ClaudeProvider.read` can price and
    collect the model set from one `_decode_lines` pass instead of decoding
    twice, and so `price_lines`' own tests exercise this function's
    `UnpricedModel` raise instead of a second, untested copy of it.
    """
    total = 0.0
    for entry in entries:
        message = entry.get("message") or {}
        usage = message.get("usage")
        if not usage:
            continue
        if not _carried_tokens(usage):
            # Zero tokens cost $0 at every rate, so a rate is not needed to
            # price this record -- and demanding one refuses a transcript over
            # a model no policy can declare. `<synthetic>` is Claude Code's
            # own pseudo-model, in 19% of local transcripts and zero-token in
            # every record of it, so `policy._validate_agents` never sees it
            # and the operator's only warning is a Drain at 2am.
            #
            # Skipped rather than priced so the two readers of a transcript
            # agree: `_claude_models` already defines an OBSERVED model as one
            # that carried nonzero tokens, and a cost path that refused
            # exactly the records the model path ignores is the drift that
            # split the two definitions in the first place.
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


def _claude_models(entries: list[dict]) -> frozenset[str]:
    out: set[str] = set()
    for entry in entries:
        message = entry.get("message") or {}
        usage = message.get("usage")
        if not usage:
            continue
        if not _carried_tokens(usage):
            continue
        model = message.get("model")
        if isinstance(model, str) and model:
            out.add(model)
    return frozenset(out)


@dataclass(frozen=True)
class ClaudeProvider:
    """Claude Code. Per-message usage deltas: the fold is a SUM."""

    projects_dir: Path
    name: str = "claude"

    def session(self, session_id: str) -> Session | None:
        """The parent transcript, plus every subagent transcript beside it.

        `transcript_path` alone globs `<projects>/*/<uuid>.jsonl` -- one level
        -- and Claude Code writes subagents one level deeper, at
        `<projects>/<slug>/<uuid>/subagents/agent-<child>.jsonl`. Measured
        over 1,204 local transcripts: 214 at the level the glob searched and
        990 (82%) below it, and across the 66 sessions holding both, 46.7% of
        all tokens sat in the half nothing read. That spend was not refused,
        which would have armed the drain -- it was simply absent, so both
        `per_task_usd` and `fleet_usd` were short by it and nothing said so.

        The glob names `subagents` rather than matching `<uuid>/*/*.jsonl`,
        because that directory has a sibling: 53 local sessions carry a
        `tool-results/` directory at the same depth, whose contents are not
        agent spend and must never be priced as any.
        """
        parent = transcript_path(session_id, self.projects_dir)
        if parent is None:
            return None
        subagents = parent.parent / session_id / "subagents"
        return Session(parent=parent, children=tuple(sorted(subagents.glob("*.jsonl"))))

    def read(self, lines: list[str], prices: Prices) -> Reading:
        entries = list(_decode_lines(lines))
        return Reading(cost_usd=_claude_cost(entries, prices), models=_claude_models(entries))


class UnpricedTranscript(Exception):
    """A transcript reports usage that cannot be decomposed into billable parts.

    Distinct from `UnpricedModel`, which names a model with no rate. This names
    a record whose rates are known but whose token breakdown is absent or
    self-contradictory -- measured on 50 of 80 local codex sessions, which
    report a cumulative `total_tokens` with every component field zero.

    Refused rather than approximated. Pricing the bare total at the output rate
    cannot under-bill but over-bills a read-heavy session several-fold and
    drains the night immediately; at the input rate it under-bills every output
    token. Both put a fabricated number inside a spending ceiling.
    """


def _codex_last_total(entries: list[dict]) -> dict | None:
    """The LAST `total_token_usage`, because codex's is a running total.

    Verified over 81 local sessions: this value never decreases, including
    across `compacted` events, so the last record is the session total and no
    reset handling is needed.
    """
    last: dict | None = None
    for entry in entries:
        if entry.get("type") != "event_msg":
            continue
        payload = entry.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        total = (payload.get("info") or {}).get("total_token_usage")
        if isinstance(total, dict):
            last = total
    return last


def _codex_models(entries: list[dict]) -> frozenset[str]:
    """Every model the session named, unfiltered by token count.

    Unlike `_claude_models`, this cannot narrow to models that "carried
    nonzero tokens" -- codex reports one cumulative total for the whole
    session with no per-model attribution, so there is no per-model token
    count to filter on. `CodexProvider.read`'s single-model refusal is what
    keeps that sound: any session naming more than one model here is refused
    before this set's contents matter.
    """
    out: set[str] = set()
    for entry in entries:
        if entry.get("type") != "turn_context":
            continue
        model = (entry.get("payload") or {}).get("model")
        if isinstance(model, str) and model:
            out.add(model)
    return frozenset(out)


def _codex_spawned_subagents(entries: list[dict]) -> bool:
    """Whether the parent's own lines say it spawned threads.

    codex mirrors its live subagent roster into
    `world_state.payload.state.environments.subagents`. Keyed on that FIELD
    being non-empty rather than on the record existing: a single local rollout
    carries 35 `world_state` records, so a guard on the type alone would
    refuse every codex session on the box.

    Nicknames, not ids -- `"- diverge_games: Rawls"`. Enough to know the
    spend is incomplete, never enough to go and find it, which is exactly why
    `read` refuses instead of summing.
    """
    for entry in entries:
        if entry.get("type") != "world_state":
            continue
        state = (entry.get("payload") or {}).get("state") or {}
        if (state.get("environments") or {}).get("subagents"):
            return True
    return False


@dataclass(frozen=True)
class CodexProvider:
    """codex. Cumulative session totals: the fold is LAST, never sum."""

    sessions_dir: Path
    name: str = "codex"

    def session(self, session_id: str) -> Session | None:
        """`<sessions>/YYYY/MM/DD/rollout-<timestamp>-<session-id>.jsonl`, or None.

        The session id is a suffix of the filename, not the whole stem, and the
        file is date-nested rather than flat -- neither is true of Claude Code's
        layout, so the glob cannot be shared.

        `children` is always empty, and that is a statement about what can be
        RESOLVED, not about what was spawned. A codex child rollout is named
        by its own thread id and records its parent only INSIDE the file, in
        `session_meta.payload.parent_thread_id`, so finding one means reading
        the first line of every rollout under `sessions_dir` -- 81 files
        locally and unbounded in general, per agent per tick. The parent's own
        lines can prove children exist far more cheaply, so `read` refuses
        that session rather than this method reporting it childless.
        """
        hits = sorted(Path(self.sessions_dir).glob(f"**/*-{session_id}.jsonl"))
        return Session(parent=hits[0]) if hits else None

    def read(self, lines: list[str], prices: Prices) -> Reading:
        entries = list(_decode_lines(lines))
        if _codex_spawned_subagents(entries):
            # Ahead of every other refusal below, because it is the largest:
            # on the one local session with children, the parent priced here
            # would be 26% of the real spend. Reporting a confident number
            # for a quarter of a session is worse than reporting none.
            raise UnpricedTranscript(
                "codex transcript reports spawned subagent threads, whose rollouts "
                "are named by the CHILD thread id and cannot be resolved from this "
                "session id. Measured on a real session: 14 children spent 37,366,303 "
                "tokens against the parent's 13,435,746, so pricing the parent alone "
                "would put 26% of the session inside a ceiling built to hold all of it."
            )
        total = _codex_last_total(entries)
        if total is None:
            # No usage record yet. A fact about timing, not an error -- same
            # reasoning as `transcript_path` returning None.
            return Reading(cost_usd=0.0, models=frozenset())

        models = _codex_models(entries)
        if not models:
            # Split from the multi-model message below because this is the
            # branch real data actually takes: all 50 local total-only
            # sessions carry no `turn_context` record at all, so every one of
            # them reaches this raise -- and telling that operator their
            # session "cannot be split between two rates" names a problem
            # they do not have and hides the one they do.
            raise UnpricedTranscript(
                f"codex transcript reports usage (total_tokens="
                f"{total.get('total_tokens') or 0}) but names no model in any "
                "`turn_context` record, so there is no rate to price it at. "
                "Pricing it at zero would disable the budget ceiling instead."
            )
        if len(models) > 1:
            raise UnpricedTranscript(
                f"codex transcript names {len(models)} models {sorted(models)!r}; its "
                "token totals are cumulative for the whole session and are not "
                "attributed per model, so they cannot be split between two rates."
            )
        model = next(iter(models))
        table = prices.get(model)
        if table is None:
            raise UnpricedModel(
                f"no price for model {model!r}. Add it to [prices] in fleet.toml. "
                "Pricing it at zero would disable the budget ceiling instead."
            )

        inp = total.get("input_tokens") or 0
        out = total.get("output_tokens") or 0
        total_tokens = total.get("total_tokens") or 0
        if inp + out != total_tokens:
            # The invariant that matters is "the components account for the
            # total", not "the components are not entirely absent" -- a guard
            # on the latter misses input_tokens=0, output_tokens=5 against a
            # total of 39,958, which prices ~40k missing tokens near zero
            # instead of refusing them. Verified over 1,358 real records:
            # every one with a breakdown satisfies this sum; the 50 without
            # one report input=output=0, which this also catches.
            raise UnpricedTranscript(
                f"codex transcript reports total_tokens={total_tokens} but "
                f"input_tokens={inp} + output_tokens={out} = {inp + out}, so the "
                "breakdown does not account for the total and cannot be trusted. "
                "Pricing a breakdown that doesn't sum to the total would put a "
                "guessed number inside the budget ceiling."
            )
        cached = total.get("cached_input_tokens") or 0
        if cached > inp:
            raise UnpricedTranscript(
                f"codex reports cached_input_tokens={cached} exceeding "
                f"input_tokens={inp}. Never observed in 1,358 real records, so the "
                "schema is not what this adapter was written against; billing the "
                "difference as a negative would read an expensive agent as a credit."
            )
        cache_write = total.get("cache_write_input_tokens") or 0
        if cache_write:
            # Nonzero in 0 of 1,358 real records, so there is no observed case
            # to calibrate against: whether it is included in input_tokens
            # (like cached reads) or additional to it is unknown, and pricing
            # it either way would be a guess inside the ceiling. Refuse rather
            # than guess, same posture as the total-only and cached-exceeding
            # refusals above.
            raise UnpricedTranscript(
                f"codex reports cache_write_input_tokens={cache_write}, never "
                "nonzero in the sessions this adapter was calibrated against, so "
                "its relationship to input_tokens is unknown and pricing it "
                "either way would be a guess inside the budget ceiling."
            )
        # `input_tokens` INCLUDES the cached ones here, unlike Claude Code's.
        # `reasoning_output_tokens` is a SUBSET of `output_tokens` and is
        # deliberately not added.
        cost = (
            (inp - cached) * table["input"] + out * table["output"] + cached * table["cache_read"]
        ) / _PER
        return Reading(cost_usd=cost, models=models)


#: The agent kinds that have a transcript reader. `build_providers` returns
#: exactly these keys; validation compares against the names alone so it needs
#: no filesystem and no home directory.
PROVIDER_KINDS = frozenset({"claude", "codex"})


def build_providers(home: Path) -> dict[str, Provider]:
    """Agent kind -> Provider, keyed by herdr's `agent` field.

    Keyed by what the snapshot actually says, not by a display name:
    `parse_snapshot` dispatches on `agent["agent"]`, and a registry keyed on
    anything else would miss every agent and read the fleet as free.
    """
    home = Path(home)
    return {
        "claude": ClaudeProvider(projects_dir=home / ".claude" / "projects"),
        "codex": CodexProvider(sessions_dir=home / ".codex" / "sessions"),
    }
