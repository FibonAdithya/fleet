from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.cost import CodexProvider, UnpricedModel, UnpricedTranscript

CAPTURED = Path(__file__).resolve().parents[1] / "fixtures/cost"


def _codex_models(lines: list[str]) -> frozenset[str]:
    """Every model a captured transcript names, read straight off raw lines.

    A test-only mirror of `fleet.cost._codex_models`, which takes decoded
    entries rather than raw lines -- kept separate (not imported) so these
    tests build their `prices` table the same way a caller with only raw
    lines would, rather than reaching into the module's private decode step.
    """
    out: set[str] = set()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "turn_context":
            continue
        model = (entry.get("payload") or {}).get("model")
        if isinstance(model, str) and model:
            out.add(model)
    return frozenset(out)


PRICES = {
    "gpt-test": {"input": 10.0, "output": 100.0, "cache_read": 1.0, "cache_write": 20.0},
}

PROVIDER = CodexProvider(sessions_dir=Path("/nonexistent"))


def _model(name="gpt-test"):
    return json.dumps({"type": "turn_context", "payload": {"model": name}})


def _tokens(**fields):
    total = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }
    total.update(fields)
    total.setdefault("total_tokens", total["input_tokens"] + total["output_tokens"])
    return json.dumps(
        {
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": total}},
        }
    )


def test_cumulative_totals_are_folded_by_last_not_sum():
    """codex reports a RUNNING TOTAL, not a per-turn delta. Catches a fold that
    sums the records: this transcript would price at 6M input instead of 3M,
    and a real 25-turn session at roughly 25x its true cost."""
    lines = [
        _model(),
        _tokens(input_tokens=1_000_000, total_tokens=1_000_000),
        _tokens(input_tokens=2_000_000, total_tokens=2_000_000),
        _tokens(input_tokens=3_000_000, total_tokens=3_000_000),
    ]
    assert PROVIDER.read(lines, PRICES).cost_usd == pytest.approx(30.0)


def test_cached_tokens_are_subtracted_from_input_not_added_to_it():
    """codex `input_tokens` INCLUDES `cached_input_tokens`; Claude Code's
    excludes them. Catches pricing the raw input field at the base rate, which
    charges cache reads at 10x here."""
    lines = [
        _model(),
        _tokens(input_tokens=1_000_000, cached_input_tokens=900_000, total_tokens=1_000_000),
    ]
    # 100k billable at $10/M + 900k cache-read at $1/M = 1.0 + 0.9
    assert PROVIDER.read(lines, PRICES).cost_usd == pytest.approx(1.9)


def test_reasoning_tokens_are_not_added_to_output():
    """`reasoning_output_tokens` is a SUBSET of `output_tokens`. Catches adding
    the two, which double-counts every reasoning token."""
    lines = [
        _model(),
        _tokens(output_tokens=1_000_000, reasoning_output_tokens=400_000, total_tokens=1_000_000),
    ]
    assert PROVIDER.read(lines, PRICES).cost_usd == pytest.approx(100.0)


def test_a_total_only_transcript_refuses_rather_than_pricing_zero():
    """50 of 80 local codex sessions report a cumulative total with every
    component zero. Catches returning 0.0, which switches the ceiling off
    silently -- the failure UnpricedModel exists to prevent, by another door."""
    lines = [_model(), _tokens(total_tokens=39_958)]
    with pytest.raises(UnpricedTranscript):
        PROVIDER.read(lines, PRICES)


def test_a_session_naming_two_models_refuses():
    """codex totals are cumulative for the whole session and are not attributed
    per model, so a session that switched models cannot be split between their
    rates. Catches silently pricing the whole session at whichever model was
    seen first."""
    lines = [
        _model("gpt-test"),
        _model("gpt-other"),
        _tokens(input_tokens=1_000_000, total_tokens=1_000_000),
    ]
    with pytest.raises(UnpricedTranscript):
        PROVIDER.read(lines, PRICES)


def test_an_unknown_model_raises_unpriced_model():
    """Same treatment the Claude adapter gives. Catches a codex path that
    defaults an unknown model to zero."""
    lines = [_model("nobody-prices-me"), _tokens(input_tokens=1_000, total_tokens=1_000)]
    with pytest.raises(UnpricedModel):
        PROVIDER.read(lines, PRICES)


def test_cached_exceeding_input_refuses():
    """Never observed in 1,358 real records, so if it appears the schema is not
    what this adapter was written against. Catches a negative billable-input
    term, which would make an expensive agent read as a credit."""
    lines = [
        _model(),
        _tokens(input_tokens=1_000, cached_input_tokens=5_000, total_tokens=1_000),
    ]
    with pytest.raises(UnpricedTranscript):
        PROVIDER.read(lines, PRICES)


def test_a_transcript_with_no_usage_yet_is_free_and_nameless():
    """Ordinary in the seconds after dispatch. Catches raising on a transcript
    that simply has not been written to yet, which would drain a healthy fleet."""
    reading = PROVIDER.read([_model()], PRICES)
    assert reading.cost_usd == 0.0
    assert reading.models == frozenset()


def test_a_total_inconsistent_with_its_components_refuses():
    """A partial breakdown that does not sum to the reported total is refused,
    not priced on whatever fragment is present. Catches a guard that only
    fires when BOTH components are exactly zero: input_tokens=0,
    output_tokens=5 against total_tokens=39_958 would price ~40k missing
    tokens at effectively zero instead of refusing them."""
    lines = [
        _model(),
        _tokens(input_tokens=0, output_tokens=5, total_tokens=39_958),
    ]
    with pytest.raises(UnpricedTranscript):
        PROVIDER.read(lines, PRICES)


def test_nonzero_cache_write_refuses():
    """cache_write_input_tokens is nonzero in 0 of 1,358 real records, so
    whether it sits inside input_tokens (like cached reads) or beside it is
    uncalibratable. Catches pricing it via either guess -- both put a number
    with no observed basis inside the spending ceiling -- and catches
    silently dropping the term, which would under-bill any session where it
    does turn out to be additional to input_tokens."""
    lines = [
        _model(),
        _tokens(input_tokens=1_000, cache_write_input_tokens=500, total_tokens=1_000),
    ]
    with pytest.raises(UnpricedTranscript):
        PROVIDER.read(lines, PRICES)


def test_codex_transcript_is_found_under_the_date_nested_layout(tmp_path):
    """codex writes `sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`, not
    `<uuid>.jsonl`. Catches reusing the Claude glob, which finds nothing and
    silently reads every codex agent as free."""
    sid = "019ff017-820a-73c1-aa2c-748d025ad0b5"
    nested = tmp_path / "2026" / "08" / "11"
    nested.mkdir(parents=True)
    target = nested / f"rollout-2026-08-11T10-11-40-{sid}.jsonl"
    target.write_text("{}\n")
    assert CodexProvider(sessions_dir=tmp_path).transcript(sid) == target


def test_a_missing_codex_transcript_is_none_not_a_crash(tmp_path):
    """Catches raising on a missing file, which would drain the fleet in the
    seconds after every dispatch when a transcript has not been written yet."""
    assert CodexProvider(sessions_dir=tmp_path).transcript("no-such-session") is None


def test_the_registry_exposes_both_providers_by_herdr_agent_kind():
    """The keys must match the `agent` field in herdr's snapshot, which is what
    parse_snapshot dispatches on. Catches a registry keyed by display name."""
    from fleet.cost import build_providers

    providers = build_providers(Path("/home/nobody"))
    assert set(providers) == {"claude", "codex"}
    assert providers["claude"].name == "claude"
    assert providers["codex"].name == "codex"


def test_provider_kinds_matches_build_providers_keys():
    """The constant and registry cannot drift apart without breaking validation.
    Catches editing one without updating the other."""
    from fleet.cost import PROVIDER_KINDS, build_providers

    providers = build_providers(Path("/home/nobody"))
    assert set(providers) == PROVIDER_KINDS


def test_codex_transcript_glob_requires_separator_before_session_id(tmp_path):
    """The glob must match only `rollout-<ts>-<uuid>.jsonl`, not a decoy
    `rollout-<ts>-XXXX<uuid>.jsonl` where the session id is a suffix of
    another file's name. Catches a bare suffix glob, which would return the
    decoy (sorted first by timestamp) and price one agent from another's
    transcript."""
    sid = "019ff017-820a-73c1-aa2c-748d025ad0b5"
    nested = tmp_path / "2026" / "08" / "11"
    nested.mkdir(parents=True)

    # Correct file: rollout-<timestamp>-<session-id>
    correct = nested / f"rollout-2026-08-11T10-11-40-{sid}.jsonl"
    correct.write_text("{}\n")

    # Decoy file: session id is a suffix of a different filename
    # This would match a bare suffix glob and sort before the correct file
    decoy = nested / f"rollout-2026-08-11T09-00-00-XXXX{sid}.jsonl"
    decoy.write_text("{}\n")

    # Must return the correct file, not the decoy
    assert CodexProvider(sessions_dir=tmp_path).transcript(sid) == correct


def test_a_real_captured_codex_session_prices():
    """Against a real shape, not an invented one -- the parent spec's rule.
    Catches an adapter that only handles the hand-written fixtures above: a
    real session's field set, record ordering, and repeated `turn_context`
    entries (this one has five, all naming the same model, across a session
    that ran many turns) differ from anything invented by hand, and this is
    the only test that reads one end to end and demands a positive cost out
    the other side."""
    lines = (CAPTURED / "codex-breakdown.jsonl").read_text().splitlines()
    models = _codex_models(lines)
    prices = {
        m: {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0} for m in models
    }
    assert CodexProvider(sessions_dir=Path("/nonexistent")).read(lines, prices).cost_usd > 0


def test_a_real_captured_total_only_session_refuses():
    """The 50-of-80 case, from a real session. Unlike every hand-written
    total-only fixture above (which pairs `_model()` with an all-zero
    breakdown), this real session names no model at all -- verified across
    all 50 local sessions of this shape, none carries a single
    `turn_context` record -- so it reaches `UnpricedTranscript` through the
    model-count guard rather than the total/breakdown-mismatch guard the
    hand-written tests above exercise. Catches an adapter that treats "no
    model named, but usage present" as "not started yet" and returns
    `Reading(0.0, frozenset())` instead of refusing -- the one branch of
    that guard no other test here reaches, because every other test's lines
    include at least one `_model()`."""
    lines = (CAPTURED / "codex-total-only.jsonl").read_text().splitlines()
    models = _codex_models(lines)
    prices = {
        m: {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0} for m in models
    }
    with pytest.raises(UnpricedTranscript):
        CodexProvider(sessions_dir=Path("/nonexistent")).read(lines, prices)


def test_a_nameless_session_says_so_rather_than_blaming_a_model_split():
    """The 0-model and 2-model refusals are one guard but two different
    operator problems, and the real-data branch is the 0-model one: all 50
    local total-only sessions carry no `turn_context` record at all, so every
    one of them lands here. Catches folding the two back into a single
    `len(models) != 1` message, which tells that operator their session
    "cannot be split between two rates" -- a problem they do not have, while
    saying nothing about the one they do."""
    lines = [_tokens(total_tokens=39_958)]
    with pytest.raises(UnpricedTranscript, match="names no model"):
        PROVIDER.read(lines, PRICES)


def test_a_two_model_session_still_names_the_split_it_cannot_make():
    """The other half of that split. Catches collapsing both branches into
    the nameless-session message, which would leave a genuine mid-session
    model substitution reported as a missing `turn_context`."""
    lines = [_model("gpt-test"), _model("gpt-other"), _tokens(input_tokens=1_000)]
    with pytest.raises(UnpricedTranscript, match="cannot be split between two rates"):
        PROVIDER.read(lines, PRICES)
