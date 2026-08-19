from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.cost import CodexProvider, UnpricedModel, UnpricedTranscript

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
