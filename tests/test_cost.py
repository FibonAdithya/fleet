from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.cost import (
    ClaudeProvider,
    Reading,
    UnpricedModel,
    price_lines,
    price_transcript,
    transcript_path,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/cost/transcript.jsonl"

PRICES = {
    "test-model": {"input": 10.0, "output": 100.0, "cache_read": 1.0, "cache_write": 20.0},
}


def _line(model="test-model", **usage):
    return json.dumps({"message": {"model": model, "usage": usage}})


def test_a_single_message_is_priced_per_million_tokens():
    """1M input at $10 plus 1M output at $100. Catches a per-thousand divisor,
    which is off by 1000 in the direction that never fires the ceiling."""
    line = _line(input_tokens=1_000_000, output_tokens=1_000_000)
    assert price_lines([line], PRICES) == pytest.approx(110.0)


def test_cache_counters_are_priced_separately():
    """Cache reads are an order of magnitude cheaper than input and cache writes
    are more expensive. Folding all four into one rate is wrong in both
    directions at once."""
    line = _line(cache_read_input_tokens=1_000_000, cache_creation_input_tokens=1_000_000)
    assert price_lines([line], PRICES) == pytest.approx(21.0)


def test_a_missing_counter_is_zero_not_a_crash():
    """Not every usage record carries all four keys."""
    assert price_lines([_line(input_tokens=1000)], PRICES) == pytest.approx(0.01)


def test_an_explicit_zero_counter_prices_at_zero_not_by_being_skipped():
    """A usage record with real, explicit zero counts (a known model that did
    no work on this line) must still be priced -- and priced at exactly zero,
    the same answer a missing counter gives, not treated as if the line had no
    usage at all. `0.0` is a legitimate price, not an absent one."""
    line = _line(
        input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )
    assert price_lines([line], PRICES) == 0.0


def test_lines_without_usage_are_skipped():
    assert price_lines([json.dumps({"type": "user"}), ""], PRICES) == 0.0


def test_a_truncated_final_line_is_skipped():
    """A transcript being appended to while the daemon reads it ends mid-line.
    Crashing there would take down a tick for a file that is merely still being
    written."""
    lines = [_line(input_tokens=1000), '{"message": {"mod']
    assert price_lines(lines, PRICES) == pytest.approx(0.01)


def test_an_unpriced_model_raises_rather_than_costing_nothing():
    """Pricing an unknown model at zero switches the budget ceiling off
    silently, and the event that triggers it -- a model changing under an agent
    -- happens without anyone editing fleet.toml."""
    with pytest.raises(UnpricedModel) as caught:
        price_lines([_line(model="some-new-model", input_tokens=1)], PRICES)
    assert "some-new-model" in str(caught.value)


def test_usage_with_no_model_named_raises():
    """Same rule from the other side: a usage record we cannot attribute is not
    a free one."""
    with pytest.raises(UnpricedModel):
        price_lines([json.dumps({"message": {"usage": {"input_tokens": 1}}})], PRICES)


def test_the_captured_transcript_prices_without_error():
    """The fidelity obligation. A pricer that only works on hand-built lines is
    a pricer that has never met the format it is for."""
    models = set()
    for line in FIXTURE.read_text().splitlines():
        entry = json.loads(line) if line.strip() else {}
        message = entry.get("message") or {}
        if message.get("usage"):
            models.add(message.get("model"))
    assert models, "the capture has no usage records; re-capture it"
    prices = {
        m: {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0} for m in models
    }
    assert price_transcript(FIXTURE, prices) > 0


def test_transcript_path_finds_a_session_by_uuid(tmp_path):
    (tmp_path / "-home-user-proj").mkdir()
    target = tmp_path / "-home-user-proj" / "abc-123.jsonl"
    target.write_text("")
    assert transcript_path("abc-123", tmp_path) == target


def test_transcript_path_returns_none_for_an_unknown_session(tmp_path):
    """An agent herdr knows about whose transcript has not been written yet.
    None is a fact about the filesystem; raising would take down the tick."""
    assert transcript_path("nope", tmp_path) is None


def test_claude_provider_reports_cost_and_models_from_one_parse():
    """One read yields both facts. Catches a provider that returns cost but an
    empty model set, which would make every declared-vs-observed check pass
    vacuously."""
    lines = [
        _line(model="alpha", input_tokens=1_000_000),
        _line(model="beta", output_tokens=1_000_000),
    ]
    prices = {
        "alpha": {"input": 10.0, "output": 100.0, "cache_read": 1.0, "cache_write": 20.0},
        "beta": {"input": 10.0, "output": 100.0, "cache_read": 1.0, "cache_write": 20.0},
    }
    reading: Reading = ClaudeProvider(Path("/nonexistent")).read(lines, prices)
    assert reading.cost_usd == pytest.approx(110.0)
    assert reading.models == frozenset({"alpha", "beta"})


def test_claude_provider_omits_zero_token_models_from_the_observed_set():
    """`<synthetic>` carries a usage block whose counters are all zero. It must
    be priced (its key is required) but must NOT count as an observed model, or
    every run reports a mismatch against its declared model."""
    prices = {
        "test-model": {"input": 10.0, "output": 100.0, "cache_read": 1.0, "cache_write": 20.0},
        "<synthetic>": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0},
    }
    lines = [
        _line(model="test-model", input_tokens=1_000_000),
        _line(model="<synthetic>", input_tokens=0, output_tokens=0),
    ]
    reading: Reading = ClaudeProvider(Path("/nonexistent")).read(lines, prices)
    assert reading.models == frozenset({"test-model"})


def test_a_zero_token_model_needs_no_price_row():
    """`<synthetic>` is Claude Code's own pseudo-model, present in 38 of 199
    local transcripts (19%) and carrying a usage block whose every counter is
    zero in all 72 records of it found there. It is never a model any policy
    DECLARES, so `policy._validate_agents` -- the startup gate this branch
    adds precisely to move unpriced-model failures ahead of any spending --
    structurally cannot ask for a rate for it.

    Demanding one anyway is a false positive with no upside: zero tokens cost
    $0 at every rate, so the raise reports nothing an operator can act on
    while `parse_snapshot` discards the real cost already computed for the
    same transcript, blanks the observed model, and arms `Daemon.tick`'s
    Drain -- stopping every Dispatch and Continue for the rest of the night
    on one in five Claude sessions.

    Catches restoring the unconditional `prices.get(model)` lookup: the
    predicate here is the same one `_claude_models` already uses to decide
    which models were OBSERVED, and the two must agree on what "carried
    tokens" means or the cost path refuses exactly the records the model path
    ignores."""
    zero = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    lines = [
        _line(model="test-model", input_tokens=1_000_000),
        _line(model="<synthetic>", **zero),
        # A second zero-token record under a name that is NOT `<synthetic>`
        # and has no [prices] row either. Without it this test passes against
        # a fix that special-cases the literal string, which would refuse the
        # next zero-token pseudo-model Claude Code introduces under a
        # different name -- and would still disagree with `_claude_models`,
        # which ignores every zero-token record whatever it is called.
        _line(model="some-other-zero-token-pseudo-model", **zero),
    ]
    assert price_lines(lines, PRICES) == pytest.approx(10.0)
