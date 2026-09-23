# Multi-Provider Cost Accounting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the fleet run agents on more than one CLI, priced correctly, with the agent and model for each role declared in policy and verified against what actually ran.

**Architecture:** A `Provider` seam in `cost.py` owns each CLI's transcript location and its *fold* (Claude Code sums per-message deltas; codex takes the last cumulative total). Policy gains a `Role | Agent | Model` table validated against `[prices]` at load. `AgentState` carries the observed model; `decide` compares it to the declared one and escalates the task on a mismatch.

**Tech Stack:** Python 3.13, `uv`, pytest, ruff. No new dependencies.

**Spec:** `docs/ai/specs/2026-08-19-multi-provider-cost-design.md`

## Global Constraints

- **Never price anything at zero as a fallback.** A missing rate, an unknown model, or an unparseable breakdown raises. `cost.py`'s existing rule: "Pricing it at zero would disable the budget ceiling instead."
- **`price` implementations are pure functions of lines.** Only `transcript()` touches the filesystem.
- **Fixtures are captured from real sessions, never invented.** New codex fixtures come from `~/.codex/sessions/`, sanitised of prompt text.
- **Every test names the mutation it catches**, in its docstring, following the existing style in `tests/test_cost.py`.
- **Existing `tests/test_cost.py` must pass unchanged.** `price_lines`, `price_transcript`, and `transcript_path` keep their current signatures and behaviour; the providers wrap them.
- Commit after every task. Stage explicit paths — never `git add -A`.
- `make check` (ruff + ruff format + pytest) must pass before each commit.

### Deviation from the spec, decided during planning

The spec's §2 protocol has two methods (`transcript`, `price`). This plan uses
**one read method returning both facts**:

```python
def read(self, lines: list[str], prices: Prices) -> Reading
```

Reason: §4's declared-vs-observed check needs the model set *and* the cost, and
both come from the same parse. Two methods would parse every transcript twice
per agent per tick. `Reading` is a frozen dataclass carrying `cost_usd` and
`models`. This is a strict superset of the spec's intent, not a change to it.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/fleet/cost.py` | Pricing primitives, `Provider` protocol, both adapters, registry | Modify (additive) |
| `src/fleet/config.py` | `[agents]` section → `AgentSpec` | Modify |
| `src/fleet/policy.py` | Parse + validate the `Role \| Agent \| Model` table | Modify |
| `src/fleet/model.py` | `AgentState.model`, `Policy.agents`, `AgentSpec` | Modify |
| `src/fleet/herdr.py` | Per-role launch, provider-driven `parse_snapshot` | Modify |
| `src/fleet/decide.py` | `model_mismatch` escalate branch | Modify |
| `tests/test_cost.py` | Existing Claude tests (unchanged) + provider tests | Modify |
| `tests/test_codex_cost.py` | Codex adapter tests | Create |
| `tests/test_policy.py` | Agent-table parsing and validation | Modify |
| `tests/test_decide_reap.py` | Mismatch escalation | Modify |
| `fixtures/cost/codex-breakdown.jsonl` | Real codex session with breakdowns | Create |
| `fixtures/cost/codex-total-only.jsonl` | Real codex session without breakdowns | Create |
| `fixtures/policy/fleet-fixture/docs/agent/task-classes.md` | Gains the agent table | Modify |
| `fixtures/policy/fleet-fixture/fleet.toml` | Gains `[agents]` and `[prices]` | Modify |

---

### Task 1: `Reading`, `Provider` protocol, and `ClaudeProvider`

Additive only. Nothing changes behaviour; existing tests must pass untouched.

**Files:**
- Modify: `src/fleet/cost.py`
- Test: `tests/test_cost.py`

**Interfaces:**
- Consumes: existing `price_lines`, `transcript_path`, `_COUNTERS`, `UnpricedModel`
- Produces:
  - `Prices = dict[str, dict[str, float]]`
  - `Reading(cost_usd: float, models: frozenset[str])`
  - `Provider` protocol: `name: str`, `transcript(session_id: str) -> Path | None`, `read(lines: list[str], prices: Prices) -> Reading`
  - `ClaudeProvider(projects_dir: Path)`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cost.py`:

```python
from fleet.cost import ClaudeProvider, Reading


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
    reading = ClaudeProvider(Path("/nonexistent")).read(lines, prices)
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
    reading = ClaudeProvider(Path("/nonexistent")).read(lines, prices)
    assert reading.models == frozenset({"test-model"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cost.py -k claude_provider -v`
Expected: FAIL with `ImportError: cannot import name 'ClaudeProvider'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/fleet/cost.py`, after `transcript_path`:

```python
from dataclasses import dataclass
from typing import Protocol

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


class Provider(Protocol):
    """One agent CLI's transcript format.

    `read` is pure: a function of lines and rates, nothing else. Only
    `transcript` touches the filesystem. That split is the same one the module
    docstring already defends -- the ceiling is the part most likely to be
    quietly wrong, so it is the part that stays a pure function.
    """

    name: str

    def transcript(self, session_id: str) -> Path | None: ...

    def read(self, lines: list[str], prices: Prices) -> Reading: ...


def _claude_models(lines: list[str]) -> frozenset[str]:
    out: set[str] = set()
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
        message = entry.get("message") or {}
        usage = message.get("usage")
        if not usage:
            continue
        if not any((usage.get(field) or 0) for field in _COUNTERS.values()):
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

    def transcript(self, session_id: str) -> Path | None:
        return transcript_path(session_id, self.projects_dir)

    def read(self, lines: list[str], prices: Prices) -> Reading:
        return Reading(cost_usd=price_lines(lines, prices), models=_claude_models(lines))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cost.py -v`
Expected: PASS, including every pre-existing test unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/fleet/cost.py tests/test_cost.py
git commit -m "feat(cost): add the Provider seam and ClaudeProvider

Additive. price_lines and transcript_path keep their signatures and
behaviour; ClaudeProvider wraps them and adds the observed-model set,
defined as models carrying nonzero tokens so <synthetic> is excluded
without a special case."
```

---

### Task 2: `CodexProvider.read` — the fold, the decomposition, the refusals

The risky task. Every trap in the spec's §1 lives here.

**Files:**
- Modify: `src/fleet/cost.py`
- Create: `tests/test_codex_cost.py`

**Interfaces:**
- Consumes: `Prices`, `Reading`, `UnpricedModel` from Task 1
- Produces: `UnpricedTranscript(Exception)`, `CodexProvider(sessions_dir: Path)` with `read`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_codex_cost.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_codex_cost.py -v`
Expected: FAIL with `ImportError: cannot import name 'CodexProvider'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/fleet/cost.py`:

```python
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


def _codex_last_total(lines: list[str]) -> dict | None:
    """The LAST `total_token_usage`, because codex's is a running total.

    Verified over 81 local sessions: this value never decreases, including
    across `compacted` events, so the last record is the session total and no
    reset handling is needed.
    """
    last: dict | None = None
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "event_msg":
            continue
        payload = entry.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        total = (payload.get("info") or {}).get("total_token_usage")
        if isinstance(total, dict):
            last = total
    return last


def _codex_models(lines: list[str]) -> frozenset[str]:
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


@dataclass(frozen=True)
class CodexProvider:
    """codex. Cumulative session totals: the fold is LAST, never sum."""

    sessions_dir: Path
    name: str = "codex"

    def transcript(self, session_id: str) -> Path | None:
        raise NotImplementedError  # Task 3

    def read(self, lines: list[str], prices: Prices) -> Reading:
        total = _codex_last_total(lines)
        if total is None:
            # No usage record yet. A fact about timing, not an error -- same
            # reasoning as `transcript_path` returning None.
            return Reading(cost_usd=0.0, models=frozenset())

        models = _codex_models(lines)
        if len(models) != 1:
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
        if inp == 0 and out == 0 and (total.get("total_tokens") or 0) > 0:
            raise UnpricedTranscript(
                f"codex transcript reports {total.get('total_tokens')} total tokens "
                "with no input/output breakdown, so it cannot be priced. Measured on "
                "50 of 80 local sessions, all with an absent session_meta."
                "thread_source. Pricing the bare total would put a guessed rate "
                "inside the budget ceiling."
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
        # `input_tokens` INCLUDES the cached ones here, unlike Claude Code's.
        # `reasoning_output_tokens` is a SUBSET of `output_tokens` and is
        # deliberately not added.
        cost = (
            (inp - cached) * table["input"]
            + out * table["output"]
            + cached * table["cache_read"]
            + cache_write * table["cache_write"]
        ) / _PER
        return Reading(cost_usd=cost, models=models)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_codex_cost.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Mutation-check the fold**

The mutation must actually **sum**. A merge such as
`last = {**(last or {}), **total}` replaces keys rather than adding them and
leaves every test passing, which would prove nothing. Use this in
`_codex_last_total`:

```python
        if isinstance(total, dict):
            if last is None:
                last = dict(total)
            else:
                last = {k: (last.get(k) or 0) + (v or 0) for k, v in total.items()}
```

Run `uv run pytest tests/test_codex_cost.py -v` and confirm
`test_cumulative_totals_are_folded_by_last_not_sum` FAILS with 60.0 against an
expected 30.0. Restore the file with `git checkout -- src/fleet/cost.py` and
re-run to confirm green.

- [ ] **Step 6: Commit**

```bash
git add src/fleet/cost.py tests/test_codex_cost.py
git commit -m "feat(cost): add CodexProvider pricing

codex reports a cumulative running total where Claude Code reports
per-message deltas, so the fold is last-not-sum; its input_tokens
includes cached tokens where Claude Code's excludes them; and
reasoning_output_tokens is a subset of output_tokens. A transcript with
no breakdown, or naming two models, is refused rather than approximated."
```

---

### Task 3: `CodexProvider.transcript` and the provider registry

**Files:**
- Modify: `src/fleet/cost.py`
- Modify: `tests/test_codex_cost.py`

**Interfaces:**
- Consumes: `CodexProvider` from Task 2, `ClaudeProvider` from Task 1
- Produces: `build_providers(home: Path) -> dict[str, Provider]` returning `{"claude": ..., "codex": ...}`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_codex_cost.py`:

```python
from pathlib import Path

from fleet.cost import build_providers


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
    """Ordinary in the seconds after dispatch."""
    assert CodexProvider(sessions_dir=tmp_path).transcript("no-such-session") is None


def test_the_registry_exposes_both_providers_by_herdr_agent_kind():
    """The keys must match the `agent` field in herdr's snapshot, which is what
    parse_snapshot dispatches on. Catches a registry keyed by display name."""
    providers = build_providers(Path("/home/nobody"))
    assert set(providers) == {"claude", "codex"}
    assert providers["claude"].name == "claude"
    assert providers["codex"].name == "codex"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_codex_cost.py -k "transcript or registry" -v`
Expected: FAIL — `NotImplementedError` and `ImportError: cannot import name 'build_providers'`

- [ ] **Step 3: Write minimal implementation**

Replace `CodexProvider.transcript` in `src/fleet/cost.py`:

```python
    def transcript(self, session_id: str) -> Path | None:
        """`<sessions>/YYYY/MM/DD/rollout-<timestamp>-<session-id>.jsonl`, or None.

        The session id is a suffix of the filename, not the whole stem, and the
        file is date-nested rather than flat -- neither is true of Claude Code's
        layout, so the glob cannot be shared.
        """
        hits = sorted(Path(self.sessions_dir).glob(f"**/*{session_id}.jsonl"))
        return hits[0] if hits else None
```

Add at the end of `src/fleet/cost.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_codex_cost.py -v && uv run pytest tests/test_cost.py -v`
Expected: PASS, all.

- [ ] **Step 5: Commit**

```bash
git add src/fleet/cost.py tests/test_codex_cost.py
git commit -m "feat(cost): locate codex transcripts and register both providers

codex nests sessions by date and suffixes the session id onto a rollout
filename, so the Claude glob cannot be shared. The registry is keyed by
herdr's own `agent` field because that is what parse_snapshot dispatches on."
```

---

### Task 4: `[agents]` in `fleet.toml`

**Files:**
- Modify: `src/fleet/config.py`
- Modify: `src/fleet/model.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `AgentSpec(kind: str, model_flag: str, args: tuple[str, ...])` in `model.py`; `FleetConfig.agents: dict[str, AgentSpec]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_agents_section_is_parsed_per_kind(tmp_path):
    """Launch flags are CLI-specific: `--permission-mode acceptEdits` means
    nothing to codex. Catches one global arg list shared across kinds."""
    (tmp_path / "fleet.toml").write_text(
        BASE
        + '\n[agents.claude]\nmodel_flag = "--model"\nargs = ["--permission-mode", "acceptEdits"]\n'
        + '\n[agents.codex]\nmodel_flag = "--model"\nargs = ["--full-auto"]\n'
    )
    agents = load_config(tmp_path).agents
    assert agents["codex"].args == ("--full-auto",)
    assert agents["claude"].model_flag == "--model"


def test_an_agent_without_a_model_flag_is_refused(tmp_path):
    """The model is always appended as [model_flag, model]. Without a flag the
    agent would silently run the operator's default model -- the exact
    silent-success the declared-model design exists to close."""
    (tmp_path / "fleet.toml").write_text(BASE + '\n[agents.codex]\nargs = ["--full-auto"]\n')
    with pytest.raises(ConfigError, match="model_flag"):
        load_config(tmp_path)
```

Add near the top of `tests/test_config.py` if not already present:

```python
BASE = (
    '[repo]\nslug = "o/r"\n\n[policy]\n'
    'ownership = "docs/agent/ownership.md"\n'
    'task_classes = "docs/agent/task-classes.md"\n'
    'roles = ".agents/roles"\nschemas = ".agents/schemas"\n'
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k agent -v`
Expected: FAIL with `AttributeError: 'FleetConfig' object has no attribute 'agents'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/fleet/model.py`:

```python
@dataclass(frozen=True)
class AgentSpec:
    """How to launch one agent CLI. Mechanics, not policy.

    `model_flag` is a separate field rather than a placeholder inside `args` so
    the launcher can always append `[model_flag, model]`. A placeholder someone
    forgets to include fails by running the operator's default model with no
    error at all.
    """

    kind: str
    model_flag: str
    args: tuple[str, ...]
```

Add to `src/fleet/config.py`:

```python
def _agents(raw: dict) -> dict[str, AgentSpec]:
    out: dict[str, AgentSpec] = {}
    for kind, table in raw.items():
        flag = table.get("model_flag")
        if not flag:
            raise ConfigError(
                f"[agents.{kind}] has no model_flag. The declared model is always "
                "appended as [model_flag, model]; without it the agent runs the "
                "operator's default model and nothing says so."
            )
        out[kind] = AgentSpec(
            kind=kind,
            model_flag=str(flag),
            args=tuple(str(a) for a in table.get("args", ())),
        )
    return out
```

Add `agents: dict[str, AgentSpec]` to the `FleetConfig` dataclass (after `prices`), and in `load_config`:

```python
        agents=_agents(data.get("agents", {})),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fleet/config.py src/fleet/model.py tests/test_config.py
git commit -m "feat(config): parse [agents] launch mechanics per CLI kind

model_flag is required rather than optional: the model is always appended
as [model_flag, model], and an agent with no flag would silently run the
operator's default model."
```

---

### Task 5: Parse the `Role | Agent | Model` policy table

**Files:**
- Modify: `src/fleet/policy.py`
- Modify: `src/fleet/model.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: existing `_rows`, `_unticked`, `known_roles`, `PolicyError`
- Produces: `parse_agents(text, roles) -> tuple[tuple[str, str, str], ...]` as `(role, agent, model)`; `Policy.agents` plus `Policy.agent_for(role) -> tuple[str, str] | None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_policy.py`:

```python
from fleet.policy import parse_agents

ROLES = frozenset({"implementer", "reviewer", "integrator", "planner"})

AGENT_TABLE = """
| Role | Agent | Model |
|---|---|---|
| `implementer` | `codex` | `gpt-5.6-sol` |
| `reviewer` | `claude` | `claude-opus-5` |
"""


def test_the_agent_table_is_parsed_into_role_agent_model_triples():
    """Catches a parser that reads only two columns and drops the model, which
    would leave every role on the operator's default model."""
    assert parse_agents(AGENT_TABLE, ROLES) == (
        ("implementer", "codex", "gpt-5.6-sol"),
        ("reviewer", "claude", "claude-opus-5"),
    )


def test_an_unknown_role_in_the_agent_table_raises():
    """Same strictness parse_routes applies to an unknown class. Catches
    skipping the row, which leaves that role silently unrouted."""
    text = "\n| Role | Agent | Model |\n|---|---|---|\n| `nobody` | `claude` | `m` |\n"
    with pytest.raises(PolicyError, match="nobody"):
        parse_agents(text, ROLES)


def test_a_missing_agent_table_raises():
    """Required, not defaulted to all-claude -- the reasoning the Policy
    docstring gives for integration_branch having no default. Catches an empty
    tuple, which would make every downstream lookup return None."""
    with pytest.raises(PolicyError, match="no role/agent"):
        parse_agents("# no tables here\n", ROLES)


def test_a_role_listed_twice_raises():
    """Catches last-wins, which silently discards the first assignment."""
    text = (
        "\n| Role | Agent | Model |\n|---|---|---|\n"
        "| `reviewer` | `claude` | `a` |\n| `reviewer` | `codex` | `b` |\n"
    )
    with pytest.raises(PolicyError, match="reviewer"):
        parse_agents(text, ROLES)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_policy.py -k agent -v`
Expected: FAIL with `ImportError: cannot import name 'parse_agents'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/fleet/policy.py`:

```python
def parse_agents(
    text: str, roles: frozenset[str]
) -> tuple[tuple[str, str, str], ...]:
    """The `Role | Agent | Model` table: which CLI and model each role runs on.

    Required, not defaulted. A Policy built without it would run every role on
    whatever CLI and model the operator's shell happened to default to, which is
    the same class of mistake `integration_branch` has no default for.

    `_rows` matches `cells[:len(header)]`, so the three-column header needs no
    parser change, and ownership.md already carries two tables this way.
    """
    rows = _rows(text, ("role", "agent", "model"))
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for cells in rows:
        if len(cells) < 3:
            raise PolicyError(f"malformed role/agent row: {cells!r}")
        role, agent, model = (_unticked(c) for c in cells[:3])
        if role not in roles:
            raise PolicyError(f"unknown role {role!r} in the role/agent table")
        if not agent or not model:
            raise PolicyError(f"role {role!r} has no agent or no model")
        if role in seen:
            raise PolicyError(
                f"role {role!r} is listed twice in the role/agent table; "
                "last-wins would silently discard the first assignment"
            )
        seen.add(role)
        out.append((role, agent, model))
    if not out:
        raise PolicyError(
            "no role/agent table found; every role would fall back to the "
            "operator's default CLI and model, unrecorded"
        )
    return tuple(out)
```

Add to `Policy` in `src/fleet/model.py`, after `routes`:

```python
    agents: tuple[tuple[str, str, str], ...]  # (role, agent kind, model)

    def agent_for(self, role: str) -> tuple[str, str] | None:
        """(agent kind, model) for a role, or None when the policy has none."""
        for name, kind, model in self.agents:
            if name == role:
                return kind, model
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: FAIL — `agents` is a required field with no default, so every existing
`Policy(...)` construction breaks. There are exactly three outside `policy.py`;
add `agents=()` to each, since none of them exercises routing:

- `tests/test_model.py:36` — `Policy(**{**fields, **kw})`; add `agents: ()` to the `fields` dict
- `tests/test_decide_reap.py:24` — the module-level `POLICY` constant
- any `Policy(` added by earlier tasks in this plan

Run the **whole** suite, not just `test_policy.py` — the other two live in files
this task does not otherwise touch, and a task-scoped run would report green
while the suite is red.

- [ ] **Step 5: Commit**

```bash
git add src/fleet/policy.py src/fleet/model.py tests/test_policy.py
git commit -m "feat(policy): parse the Role|Agent|Model table

Required rather than defaulted to all-claude: an absent table would run
every role on whatever the operator's shell defaulted to. Duplicate roles
raise rather than last-wins."
```

---

### Task 6: Cross-validate the table against prices and providers

The task that turns a 2am drain into a startup error.

**Files:**
- Modify: `src/fleet/policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `parse_agents` (Task 5), `build_providers` (Task 3), `FleetConfig.prices`
- Produces: `policy.load(cfg)` raising `PolicyError` on an unpriced model or unknown agent kind

- [ ] **Step 1: Write the failing test**

Append to `tests/test_policy.py`:

```python
def test_a_declared_model_with_no_price_is_refused_at_load(tmp_path):
    """docs/m3-inputs.md §1.2: an unpriced model is currently discovered only
    when a dispatched agent's transcript is read, mid-run, after spending.
    Catches deferring that to runtime now that the model is declared."""
    cfg = _config_with_agent_table(tmp_path, agent="claude", model="unpriced-model")
    with pytest.raises(PolicyError, match="unpriced-model"):
        policy_module.load(cfg)


def test_a_declared_agent_with_no_provider_is_refused_at_load(tmp_path):
    """Catches routing a role to a CLI whose transcripts nothing can read,
    which would drain on the first tick instead of failing at startup."""
    cfg = _config_with_agent_table(tmp_path, agent="nosuchcli", model="claude-opus-5")
    with pytest.raises(PolicyError, match="nosuchcli"):
        policy_module.load(cfg)
```

Add the helper to `tests/test_policy.py`:

```python
def _config_with_agent_table(tmp_path, agent: str, model: str):
    """A FleetConfig whose fixture policy declares one role on `agent`/`model`."""
    import shutil

    root = tmp_path / "repo"
    shutil.copytree(FIXTURE_POLICY, root)
    classes = root / "docs/agent/task-classes.md"
    classes.write_text(
        classes.read_text()
        + f"\n\n| Role | Agent | Model |\n|---|---|---|\n| `implementer` | `{agent}` | `{model}` |\n"
    )
    # A fresh fleet.toml, NOT an append. Task 10 adds [prices."claude-opus-5"]
    # to the fixture, and tomllib rejects a duplicate table outright
    # (`Cannot declare ('prices', 'm') twice`), so appending here would make
    # this helper start raising TOMLDecodeError the moment Task 10 lands.
    (root / "fleet.toml").write_text(
        '[repo]\nslug = "FibonAdithya/fleet-fixture"\n'
        'integration_branch = "integration"\n\n'
        "[policy]\n"
        'ownership = "docs/agent/ownership.md"\n'
        'task_classes = "docs/agent/task-classes.md"\n'
        'roles = ".agents/roles"\nschemas = ".agents/schemas"\n\n'
        '[agents.claude]\nmodel_flag = "--model"\nargs = []\n\n'
        '[prices."claude-opus-5"]\n'
        "input = 5.0\noutput = 25.0\ncache_read = 0.5\ncache_write = 10.0\n"
    )
    return load_config(root)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_policy.py -k refused_at_load -v`
Expected: FAIL — `load` currently raises nothing.

- [ ] **Step 3: Write minimal implementation**

In `src/fleet/policy.py`, add the import and extend `load`:

```python
from fleet.cost import PROVIDER_KINDS
```

`PROVIDER_KINDS` rather than `set(build_providers(Path.home()))`: validation
needs only the key set, and reaching for the real home directory makes a pure
check depend on the environment it happens to run in. Add it to `cost.py` beside
`build_providers`, and make `build_providers` return exactly these keys:

```python
#: The agent kinds that have a transcript reader. `build_providers` returns
#: exactly these keys; validation compares against the names alone so it needs
#: no filesystem and no home directory.
PROVIDER_KINDS = frozenset({"claude", "codex"})
```

```python
def _validate_agents(
    agents: tuple[tuple[str, str, str], ...], prices: dict[str, dict[str, float]]
) -> None:
    """Every declared agent has a reader; every declared model has a rate.

    The price check is the one that earns its place. Without a declared model an
    unpriced model is found only when a dispatched agent's transcript is read --
    mid-run, after spending, which is docs/m3-inputs.md §1.2's failure exactly.
    Declaring it moves that to a startup error, before anything is dispatched.
    """
    known = set(PROVIDER_KINDS)
    for role, kind, model in agents:
        if kind not in known:
            raise PolicyError(
                f"role {role!r} is routed to agent {kind!r}, which has no transcript "
                f"reader. Known: {sorted(known)}. Its agents could not be priced and "
                "the fleet would drain on the first tick."
            )
        if model not in prices:
            raise PolicyError(
                f"role {role!r} declares model {model!r}, which has no [prices] row "
                "in fleet.toml. Dispatching it would drain the run mid-night rather "
                "than failing here, before anything is spent."
            )
```

In `load`, build the agent table and validate before constructing `Policy`:

```python
def load(cfg: FleetConfig) -> Policy:
    roles = known_roles(cfg.roles_dir)
    classes_text = cfg.task_classes.read_text()
    agents = parse_agents(classes_text, roles)
    _validate_agents(agents, cfg.prices)
    return Policy(
        areas=parse_ownership(cfg.ownership.read_text()),
        routes=parse_routes(classes_text, roles),
        agents=agents,
        slots=cfg.slots,
        stall_minutes=cfg.stall_minutes,
        max_attempts=cfg.max_attempts,
        max_fix_passes=cfg.max_fix_passes,
        max_rebases=cfg.max_rebases,
        per_task_usd=cfg.per_task_usd,
        fleet_usd=cfg.fleet_usd,
        integration_branch=cfg.integration_branch,
    )
```

Note `cfg.task_classes.read_text()` is now called **once** and reused by both
`parse_agents` and `parse_routes`; today `load` reads it inline. Reading it
twice would work but doubles the I/O on every daemon start for no reason.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_policy.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fleet/policy.py tests/test_policy.py
git commit -m "feat(policy): validate declared agents and models at load

An unpriced model is currently found only when a dispatched agent's
transcript is read -- mid-run, after spending. Declaring the model moves
that to a startup error, before anything is dispatched."
```

---

### Task 7: Launch each role on its declared agent and model

**Files:**
- Modify: `src/fleet/herdr.py:370-395` (`RealHerdr.__init__`), `src/fleet/herdr.py:684-723` (`RealHerdr.start_agent`)
- Modify: `src/fleet/herdr_executor.py`
- Test: `tests/test_herdr.py`

**Interfaces:**
- Consumes: `AgentSpec` (Task 4), `Policy.agent_for` (Task 5)
- Produces: `RealHerdr(..., agents: dict[str, AgentSpec], routing: dict[str, tuple[str, str]])`; `start_agent(task, role, prompt)` unchanged in signature

- [ ] **Step 1: Write the failing test**

Append to `tests/test_herdr.py`:

```python
def test_a_role_is_started_on_its_declared_agent_and_model():
    """Catches a launcher that keeps one global agent_kind, which would run
    every role on claude no matter what policy declares."""
    herdr = _recording_herdr(
        agents={
            "codex": AgentSpec(kind="codex", model_flag="--model", args=("--full-auto",)),
        },
        routing={"implementer": ("codex", "gpt-5.6-sol")},
    )
    herdr.start_agent(7, "implementer", "do the thing")
    start = [c for c in herdr.calls if c[:3] == ("herdr", "agent", "start")][0]
    assert "--kind" in start and start[start.index("--kind") + 1] == "codex"
    assert "--model" in start and start[start.index("--model") + 1] == "gpt-5.6-sol"
    assert "--full-auto" in start


def test_a_role_with_no_declared_agent_raises_before_launching():
    """Catches falling back to a default kind, which would start an agent whose
    transcript the fleet cannot attribute to a declared model."""
    herdr = _recording_herdr(agents={}, routing={})
    with pytest.raises(HerdrError, match="reviewer"):
        herdr.start_agent(7, "reviewer", "do the thing")
    assert not [c for c in herdr.calls if c[:3] == ("herdr", "agent", "start")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_herdr.py -k declared -v`
Expected: FAIL — `RealHerdr.__init__` takes no `routing`.

- [ ] **Step 3: Write minimal implementation**

Replace the `agent_kind` / `agent_args` parameters in `RealHerdr.__init__`:

```python
        agents: dict[str, AgentSpec] | None = None,
        routing: dict[str, tuple[str, str]] | None = None,
```

```python
        self.agents = dict(agents or {})
        #: role -> (agent kind, model), from the policy layer.
        self.routing = dict(routing or {})
```

There is **no `_start` method** — the launch code lives inside
`RealHerdr.start_agent` (`herdr.py:684`), which already takes `role` as its
second parameter. Nothing needs threading through. Replace the `start = [...]`
block inside `start_agent`:

```python
        spec_for_role = self.routing.get(role)
        if spec_for_role is None:
            raise HerdrError(
                f"role {role!r} has no declared agent; policy's role/agent table "
                "must name one. Starting it on a default would produce a "
                "transcript the fleet cannot attribute to a declared model."
            )
        kind, model = spec_for_role
        spec = self.agents.get(kind)
        if spec is None:
            raise HerdrError(f"no [agents.{kind}] launch config in fleet.toml")
        start = ["herdr", "agent", "start", f"fleet-{task}", "--kind", kind, "--pane", pane]
        # The model is appended unconditionally: never a placeholder someone can
        # forget, because forgetting runs the operator's default model silently.
        extra = [*spec.args, spec.model_flag, model]
        start += ["--", *extra]
        self._run(*start)
```

In `src/fleet/cli.py`, build both mappings when constructing `RealHerdr`:

```python
    herdr = RealHerdr(
        worktree_root=cfg.worktree_root,
        projects_dir=Path.home() / ".claude" / "projects",
        prices=cfg.prices,
        repo_root=cfg.root,
        agents=cfg.agents,
        routing={role: (kind, model) for role, kind, model in policy.agents},
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_herdr.py tests/test_herdr_executor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fleet/herdr.py src/fleet/herdr_executor.py src/fleet/cli.py tests/test_herdr.py
git commit -m "feat(herdr): launch each role on its declared agent and model

The model is appended unconditionally as [model_flag, model] rather than
interpolated into args, so it cannot be silently omitted. A role with no
declared agent raises before launching rather than falling back."
```

---

### Task 8: Observe the model each agent actually ran

**Files:**
- Modify: `src/fleet/model.py` (`AgentState`), `src/fleet/herdr.py` (`parse_snapshot`, `RealHerdr.agents`)
- Test: `tests/test_herdr.py`

**Interfaces:**
- Consumes: `build_providers` (Task 3), `Reading` (Task 1)
- Produces: `AgentState.model: str | None`; `parse_snapshot(..., providers: dict[str, Provider] | None = None)`

**Breaking signature change — read this first.** `parse_snapshot`'s
`projects_dir` parameter is replaced by `providers`. A Claude-only provider map
is no longer derivable from a bare path, and keeping `projects_dir` as a shim
would be exactly the permissive fallback this module's docstring argues against.
Every existing call site must be migrated in this task:

- `src/fleet/herdr.py` — `RealHerdr.agents`
- `tests/test_herdr.py` — lines 202, 225, 985, 1032, 1053, 1135, 1154

Each becomes `providers={"claude": ClaudeProvider(projects_dir=<the old path>)}`.
The global constraint that existing tests pass unchanged is scoped to
`tests/test_cost.py`; `tests/test_herdr.py` changes here by design.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_herdr.py`, reusing the file's existing `_snapshot`,
`_agent`, and `ROOT` helpers:

```python
from fleet.cost import ClaudeProvider

PRICED = {"test-model": {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0}}


def _claude_transcript(tmp_path, session, model="test-model", tokens=1_000):
    """A one-message Claude transcript, mirroring `_unpriced_transcript` above."""
    project = tmp_path / "projects" / "slug"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session}.jsonl").write_text(
        json.dumps({"message": {"model": model, "usage": {"input_tokens": tokens}}}) + "\n"
    )
    return {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}


def test_parse_snapshot_records_the_model_a_transcript_names(tmp_path):
    """decide compares this against the declared model. Catches leaving it None,
    which makes every mismatch check pass vacuously and lets a silent model
    substitution run the whole night unnoticed."""
    session = "55555555-5555-5555-5555-555555555555"
    providers = _claude_transcript(tmp_path, session)
    agent = _agent(str(ROOT / "5-slug"), agent="claude", agent_session={"value": session})
    parsed = parse_snapshot(_snapshot([agent]), ROOT, providers=providers, prices=PRICED)
    assert parsed[0].model == "test-model"


def test_a_model_carrying_no_tokens_is_not_recorded_as_the_observed_model(tmp_path):
    """`<synthetic>` has a usage block of all zeros. Catches recording it as the
    observed model, which reports a mismatch against every declared model."""
    session = "66666666-6666-6666-6666-666666666666"
    project = tmp_path / "projects" / "slug"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session}.jsonl").write_text(
        json.dumps({"message": {"model": "test-model", "usage": {"input_tokens": 1000}}})
        + "\n"
        + json.dumps({"message": {"model": "<synthetic>", "usage": {"input_tokens": 0}}})
        + "\n"
    )
    prices = dict(PRICED)
    prices["<synthetic>"] = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    providers = {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}
    agent = _agent(str(ROOT / "5-slug"), agent="claude", agent_session={"value": session})
    parsed = parse_snapshot(_snapshot([agent]), ROOT, providers=providers, prices=prices)
    assert parsed[0].model == "test-model"


def test_an_agent_on_an_unregistered_cli_is_collected_not_priced_at_zero(tmp_path):
    """Defence in depth behind the load-time check. Catches reading an unknown
    CLI's agent as free, which leaves the ceiling off for that agent while every
    other agent reports normally."""
    session = "77777777-7777-7777-7777-777777777777"
    providers = _claude_transcript(tmp_path, session)
    agent = _agent(str(ROOT / "7-slug"), agent="nosuchcli", agent_session={"value": session})
    unpriced: set[str] = set()
    parsed = parse_snapshot(
        _snapshot([agent]), ROOT, providers=providers, prices=PRICED, unpriced=unpriced
    )
    assert parsed[0].cost_usd == 0.0
    assert parsed[0].model is None
    assert any("nosuchcli" in message for message in unpriced)


def test_an_unregistered_cli_raises_when_nobody_is_collecting(tmp_path):
    """Mirrors the UnpricedModel rule directly above it: a caller that forgot
    the collector must not get a fleet of free agents."""
    session = "88888888-8888-8888-8888-888888888888"
    providers = _claude_transcript(tmp_path, session)
    agent = _agent(str(ROOT / "7-slug"), agent="nosuchcli", agent_session={"value": session})
    with pytest.raises(UnpricedModel, match="nosuchcli"):
        parse_snapshot(_snapshot([agent]), ROOT, providers=providers, prices=PRICED)
```

Note `_agent` takes `**overrides`, so `agent="claude"` sets the snapshot's own
`agent` field — the key `parse_snapshot` dispatches on. The existing `_agent`
helper does not set it, so **add `"agent": "claude"` to its defaults** in the
same step; every pre-existing test then keeps exercising the Claude path.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_herdr.py -k "records_the_model or unregistered" -v`
Expected: FAIL — `AgentState` has no `model`.

- [ ] **Step 3: Write minimal implementation**

Add to `AgentState` in `src/fleet/model.py`, after `cost_usd`:

```python
    model: str | None = None
```

Extend its docstring: `model` is supplied by the shell like `task` and
`cost_usd`; it is the single model the transcript named, or None when the
transcript named none or more than one.

In `parse_snapshot`, add a `providers: dict[str, Provider] | None = None`
parameter and replace the pricing block:

```python
        cost = 0.0
        model: str | None = None
        if session and providers is not None and prices is not None:
            kind = agent.get("agent")
            provider = providers.get(kind)
            if provider is None:
                message = (
                    f"agent kind {kind!r} has no transcript reader, so its spend "
                    "cannot be counted. Reading it as free would leave the ceiling "
                    "off for this agent."
                )
                if unpriced is None:
                    raise UnpricedModel(message)
                unpriced.add(message)
            else:
                path = provider.transcript(session)
                if path is not None:
                    lines = path.read_text(encoding="utf-8").splitlines()
                    try:
                        reading = provider.read(lines, prices)
                    except (UnpricedModel, UnpricedTranscript) as exc:
                        if unpriced is None:
                            raise
                        unpriced.add(str(exc))
                    else:
                        cost = reading.cost_usd
                        if len(reading.models) == 1:
                            model = next(iter(reading.models))
                        elif len(reading.models) > 1:
                            # NOT None. None means "not known yet" and is
                            # deliberately ignored by the mismatch check, so
                            # collapsing this case into it would make a fallback
                            # substitution mid-session -- the very thing §4
                            # exists to catch -- pass vacuously.
                            model = MULTIPLE_MODELS
```

Pass `model=model` into the `AgentState(...)` construction.

Add the sentinel to `src/fleet/model.py`, beside `AgentState`:

```python
#: `AgentState.model` when a transcript named more than one model carrying
#: tokens. Distinct from None, which means "no transcript yet". A declared
#: model can never equal this string, so the mismatch check escalates it
#: without needing a special case.
MULTIPLE_MODELS = "<multiple>"
```

Update `RealHerdr.agents` to pass `self.providers`, and `RealHerdr.__init__` to
accept `providers: dict[str, Provider] | None = None`, defaulting to
`build_providers(Path.home())`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_herdr.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fleet/model.py src/fleet/herdr.py tests/test_herdr.py
git commit -m "feat(herdr): record the model each agent actually ran

parse_snapshot now dispatches on the snapshot's own `agent` field to pick
a provider, and an unregistered kind is collected into `unpriced` rather
than read as free."
```

---

### Task 9: Escalate on declared ≠ observed

**Files:**
- Modify: `src/fleet/decide.py` (`_reap`, beside the `over_budget` branch at `decide.py:371`)
- Test: `tests/test_decide_reap.py`

**Interfaces:**
- Consumes: `AgentState.model` (Task 8), `Policy.agent_for` (Task 5), `_role_for`
- Produces: `Escalate(task, reason="model_mismatch")`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_decide_reap.py`:

`decide`'s real signature is `decide(prev, ledger, policy, agents, now)` — all
five required. This file already has everything needed: `POLICY` at line 24,
the `claimed(number, attempts, state, **kw)` task helper at line 38, the
`only(intents, kind)` filter at line 51, and `dataclasses` /
`datetime` / `timezone` already imported. `FleetState()` is
default-constructible. Do **not** invent a `_task` helper — it does not exist.

```python
from fleet.model import FleetState

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
DECLARED = (("implementer", "claude", "claude-opus-5"),)


def _mismatch_case(observed_model: str | None):
    """One CLAIMED patch task with a live agent reporting `observed_model`."""
    task = claimed(1, klass="patch")
    agent = AgentState(
        task=1, status="working", change_seq=5, cost_usd=0.1, model=observed_model
    )
    policy = dataclasses.replace(POLICY, agents=DECLARED)
    intents, _ = decide(
        prev=FleetState(), ledger=[task], policy=policy, agents=[agent], now=NOW
    )
    return intents


def test_an_agent_running_a_model_nobody_declared_is_escalated():
    """UnpricedModel cannot see this: a silent substitution between two PRICED
    models fires no exception and bills the night at a rate nobody chose.
    Catches trusting the transcript as the only source of truth."""
    assert Escalate(task=1, reason="model_mismatch") in _mismatch_case("claude-opus-4-8")


def test_an_agent_running_the_declared_model_is_left_alone():
    """Catches a comparison that fires on every agent -- which would escalate
    the whole backlog on the first tick."""
    assert not [i for i in _mismatch_case("claude-opus-5") if isinstance(i, Escalate)]


def test_an_agent_whose_model_is_not_yet_known_is_left_alone():
    """A transcript not yet written reports model=None. Catches escalating
    every agent in the seconds after dispatch, when nothing is yet known."""
    assert not [i for i in _mismatch_case(None) if isinstance(i, Escalate)]


def test_an_agent_observed_running_two_models_is_escalated():
    """parse_snapshot records the sentinel MULTIPLE_MODELS when a transcript
    names more than one model carrying tokens -- a fallback substitution mid
    session. Catches collapsing that to None, which would make the very case
    §4 exists to catch pass vacuously."""
    from fleet.model import MULTIPLE_MODELS

    assert Escalate(task=1, reason="model_mismatch") in _mismatch_case(MULTIPLE_MODELS)
```

`claimed(1, klass="patch")` must produce a task whose `_role_for` resolves to
`implementer` — that is, `state="claimed"` on a `patch` route. `claimed`
defaults to `state="claimed"`, so only `klass` needs passing. If the assertion
fails because the role resolves to something else, print `_role_for(task,
policy)` before changing the production code: the branch is correct and the
fixture is what is wrong.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_decide_reap.py -k model -v`
Expected: FAIL — no `model_mismatch` intent is produced.

- [ ] **Step 3: Write minimal implementation**

In `_reap`, insert immediately **before** the `over_budget` branch:

```python
            elif _model_mismatch(task, agent, policy):
                # UnpricedModel cannot catch this: a substitution between two
                # PRICED models raises nothing and bills the night at a rate
                # nobody chose. Escalate the task rather than draining the
                # fleet -- one agent on the wrong model is a task fault, not a
                # configuration fault.
                intents.append(Escalate(task=task.number, reason="model_mismatch"))
```

Add beside `_role_for`:

```python
def _model_mismatch(task: Task, agent: AgentState, policy: Policy) -> bool:
    """True when an agent is demonstrably running a model nobody declared.

    False whenever the answer is not yet known: `model=None` is a transcript
    that has not been written, ordinary in the seconds after a dispatch, and
    escalating on it would burn the backlog on the first tick.
    """
    if agent.model is None:
        return False
    role = _role_for(task, policy)
    if role is None:
        return False
    declared = policy.agent_for(role)
    if declared is None:
        return False
    return agent.model != declared[1]
```

Ordering matters: this branch goes before `over_budget` because a wrong-model
agent's cost is measured against the wrong rate, so `over_budget` computed from
it is not a fact worth acting on.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_decide_reap.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-check the guard**

Change `if agent.model is None: return False` to `return True` and confirm
`test_an_agent_whose_model_is_not_yet_known_is_left_alone` FAILS. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/fleet/decide.py tests/test_decide_reap.py
git commit -m "feat(decide): escalate a task whose agent runs an undeclared model

Escalate rather than drain: one agent on the wrong model is a task fault.
REAPING already contains Escalate, so the agent is stopped by the existing
machinery. Ordered before over_budget, whose cost is computed at the wrong
rate when the model is wrong."
```

---

### Task 10: Fixtures, captured transcripts, and the runbook

**Files:**
- Create: `fixtures/cost/codex-breakdown.jsonl`, `fixtures/cost/codex-total-only.jsonl`
- Modify: `fixtures/policy/fleet-fixture/docs/agent/task-classes.md`, `fixtures/policy/fleet-fixture/fleet.toml`
- Modify: `docs/runbook.md`
- Test: `tests/test_codex_cost.py`

- [ ] **Step 1: Capture the two codex fixtures**

Run this to produce both, keeping only the record types the adapter reads so no
prompt text is committed:

```bash
python3 - <<'EOF'
import json, glob, pathlib
keep = {"session_meta", "turn_context", "event_msg"}
out = {"breakdown": None, "total-only": None}
for p in sorted(glob.glob(str(pathlib.Path.home() / ".codex/sessions/**/*.jsonl"), recursive=True)):
    rows, has_bd = [], False
    for raw in open(p, encoding="utf-8", errors="replace"):
        try: e = json.loads(raw)
        except ValueError: continue
        if not isinstance(e, dict) or e.get("type") not in keep: continue
        pl = e.get("payload") or {}
        if e["type"] == "event_msg" and pl.get("type") != "token_count": continue
        if e["type"] == "session_meta":
            pl = {k: pl.get(k) for k in ("cli_version", "thread_source", "model_provider")}
            e = {"type": "session_meta", "payload": pl}
        if e["type"] == "turn_context":
            e = {"type": "turn_context", "payload": {"model": pl.get("model")}}
        if e["type"] == "event_msg":
            tt = (pl.get("info") or {}).get("total_token_usage")
            if not isinstance(tt, dict): continue
            if (tt.get("input_tokens") or 0) or (tt.get("output_tokens") or 0): has_bd = True
        rows.append(e)
    if not rows: continue
    slot = "breakdown" if has_bd else "total-only"
    if out[slot] is None: out[slot] = rows
d = pathlib.Path("fixtures/cost"); d.mkdir(parents=True, exist_ok=True)
for slot, rows in out.items():
    assert rows, f"no {slot} session found"
    (d / f"codex-{slot}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(slot, len(rows), "records")
EOF
```

- [ ] **Step 2: Write the failing test against the real captures**

```python
CAPTURED = Path(__file__).resolve().parents[1] / "fixtures/cost"


def test_a_real_captured_codex_session_prices():
    """Against a real shape, not an invented one -- the parent spec's rule.
    Catches an adapter that only handles the hand-written fixtures above."""
    lines = (CAPTURED / "codex-breakdown.jsonl").read_text().splitlines()
    models = _codex_models(lines)
    prices = {m: {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0}
              for m in models}
    assert CodexProvider(sessions_dir=Path("/nonexistent")).read(lines, prices).cost_usd > 0


def test_a_real_captured_total_only_session_refuses():
    """The 50-of-80 case, from a real session. Catches an adapter that prices it
    at zero."""
    lines = (CAPTURED / "codex-total-only.jsonl").read_text().splitlines()
    models = _codex_models(lines)
    prices = {m: {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0}
              for m in models}
    with pytest.raises(UnpricedTranscript):
        CodexProvider(sessions_dir=Path("/nonexistent")).read(lines, prices)
```

- [ ] **Step 3: Run to verify, then update the fixture policy repo**

Append to `fixtures/policy/fleet-fixture/docs/agent/task-classes.md`:

```markdown
## Which agent and model each role runs on

| Role | Agent | Model |
|---|---|---|
| `implementer` | `claude` | `claude-opus-5` |
| `reviewer` | `claude` | `claude-opus-5` |
| `integrator` | `claude` | `claude-opus-5` |
| `planner` | `claude` | `claude-opus-5` |
| `scout` | `claude` | `claude-opus-5` |
| `invariant-reviewer` | `claude` | `claude-opus-5` |
```

Append to `fixtures/policy/fleet-fixture/fleet.toml`:

```toml
[agents.claude]
model_flag = "--model"
args = ["--permission-mode", "acceptEdits"]

[prices."claude-opus-5"]
input = 5.0
output = 25.0
cache_read = 0.5
cache_write = 10.0

[prices."<synthetic>"]
input = 0.0
output = 0.0
cache_read = 0.0
cache_write = 0.0
```

- [ ] **Step 4: Run the full suite**

Run: `make check`
Expected: PASS, all 720+ tests.

- [ ] **Step 5: Document the codex gate in the runbook**

Add a section to `docs/runbook.md` recording spec §6: before routing a role to
codex in a graded run, launch one codex agent through herdr and confirm its
transcript emits component breakdowns; if it does not, the run drains on the
first tick, correctly and loudly.

- [ ] **Step 6: Commit**

```bash
git add fixtures/cost/codex-breakdown.jsonl fixtures/cost/codex-total-only.jsonl \
        fixtures/policy/fleet-fixture/docs/agent/task-classes.md \
        fixtures/policy/fleet-fixture/fleet.toml \
        tests/test_codex_cost.py docs/runbook.md
git commit -m "test(cost): pin both codex transcript shapes from real captures

Captured from real sessions rather than invented, per the parent spec's
rule, and reduced to the record types the adapter reads so no prompt text
is committed. The fixture policy gains the role/agent table the loader now
requires."
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §1.1 fold difference | 1 (Claude sum), 2 (codex last) |
| §1.2 counter semantics | 2 |
| §1.3 total-only refusal | 2, 10 |
| §2 provider seam | 1, 3 |
| §3.1 policy table | 5 |
| §3.1 validation | 6 |
| §3.2 fleet.toml mechanics | 4, 7 |
| §4 declared vs observed | 8 (observe), 9 (compare) |
| §5 unknown provider | 8 |
| §6 codex gate | 10 (runbook) |
| §7 testing | every task |
| §8 out of scope | not implemented, by design |

No spec requirement is unimplemented.

**Type consistency:** `Reading(cost_usd, models)` is produced in Task 1 and
consumed in Tasks 2 and 8. `AgentSpec(kind, model_flag, args)` is produced in
Task 4 and consumed in Task 7. `Policy.agent_for(role) -> tuple[str, str] | None`
is produced in Task 5 and consumed in Tasks 7 and 9. `build_providers` is
produced in Task 3 and consumed in Tasks 6 and 8.

**Placeholder scan:** clean. Task 8's test bodies were rewritten against the
real `_snapshot` / `_agent` / `ROOT` helpers in `tests/test_herdr.py` after the
first draft left `...` in them, and the `parse_snapshot` signature change now
lists its seven call sites explicitly instead of leaving the implementer to find
them.

**One risk the plan cannot retire:** spec §6's gate. If an herdr-launched codex
session turns out not to emit component breakdowns, Task 2's refusal is correct
but codex becomes unusable under a budget ceiling, and Tasks 4–7 deliver routing
with nothing safe to route to. Run the gate before starting Task 4 — Tasks 1–3
are worth doing either way, since they also close the `<synthetic>` and
observed-model holes on the Claude path.
