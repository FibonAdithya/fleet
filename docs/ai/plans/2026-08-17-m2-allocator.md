# M2: Allocator Pure Core and Simulator — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Superseded arithmetic:** this plan's scenario YAML and test bodies were written against a captured ledger that dispatched five tasks on five free areas/slots. The shipped fixture dispatches six; see the spec's §3 invariant (`docs/ai/specs/2026-08-17-m2-allocator-design.md`) for what changed and why. This plan is left as-written — it is the record of what implementers were told — so copy a scenario block out of it expecting to update the count, not to find it already right.

**Goal:** Build `decide()` — a pure fold from ledger, policy and agent states to allocation intents — plus a scripted fake executor, a YAML scenario harness that replays whole nights offline, and `fleet plan --dry-run`.

**Architecture:** Every impure edge (gh, herdr, the clock, the filesystem) is normalized by a thin shell into frozen dataclasses. `decide()` threads its own derived memory explicitly so cross-tick bookkeeping — attempt counters, fix-pass counters, stall timers — is unit-testable. Intents describe policy; a separate pure `expand()` turns each into concrete effects, keeping `decide()`'s tests readable while still covering the label-mutation sequence.

**Tech Stack:** Python 3.10+, `click`, `jsonschema`, `PyYAML`, `tomli`, `pytest`, `ruff`. No new dependencies.

**Spec:** `docs/ai/specs/2026-08-17-m2-allocator-design.md`

## Global Constraints

- **Python floor is 3.10.** `tomllib` is stdlib only from 3.11, so `config.py`'s existing `try: import tomllib / except ModuleNotFoundError: import tomli` shim stays. Use `X | None`, not `Optional[X]`.
- **No new dependencies.** Everything needed is already in `pyproject.toml`.
- **`make check` must stay green and fast.** `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest`. Line length 100, lint rules `E,F,I,UP,B`.
- **No test touches the network.** `RealGh` is never instantiated in a test. `FakeGh` is the only client tests use.
- **Every new module lives in `src/fleet/`; every new test in `tests/`.** `pythonpath = ["src"]` is already set.
- **`decide()` and `expand()` are pure.** No `datetime.now()`, no file reads, no subprocess, no network inside either. Time enters as the `now` parameter only.
- **All domain types are `@dataclass(frozen=True)`.** Collections inside them are tuples, not lists, so instances stay hashable and cannot be mutated by a caller — the same defect class as `FakeGh` handing out its live `labels` list.
- **Commit messages** follow the repo's conventional-commit style (`feat(scope):`, `fix(scope):`, `test(scope):`, `docs:`) and end with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  ```

---

## File Structure

| File | Responsibility |
|---|---|
| `fixtures/policy/fleet-fixture/` | Vendored copy of the fixture repo's policy layer and schemas |
| `src/fleet/model.py` | Frozen domain types: `Task`, `AgentState`, `Observation`, `FleetState`, `Policy`, intents, effects |
| `src/fleet/ledger.py` | Raw gh JSON → `[Task]`. The single parity choke point |
| `src/fleet/policy.py` | `fleet.toml` + `ownership.md` + `task-classes.md` → `Policy`, strictly |
| `src/fleet/decide.py` | The pure core: REAP → UNBLOCK → DISPATCH |
| `src/fleet/expand.py` | `expand(intent, policy) → [Effect]`, pure |
| `src/fleet/executor.py` | `Executor` Protocol + `FakeExecutor` |
| `src/fleet/runlog.py` | JSONL append |
| `src/fleet/simulate.py` | YAML scenario → ticks → intents |
| `src/fleet/commands.py` | *(modify)* add `do_verdict`, bind handoff target to role |
| `src/fleet/cli.py` | *(modify)* add `verdict` and `plan` commands |
| `src/fleet/config.py` | *(modify)* parse `[budget]` |
| `tests/conftest.py` | *(modify)* point at the vendored fixtures |
| `fixtures/scenarios/*.yaml` | One scenario per parent §10 row and §11 case |

---

### Task 1: Vendor the fixture policy layer and retire the absolute path

`tests/conftest.py` points `FIXTURE_SCHEMAS` at `~/TIG/fleet-fixture/.agents/schemas` — an absolute path into another repo's *working tree*. PR #1 shipped 121 green tests that were reading a schema file no commit contained. Scenarios need a policy seed anyway, so vendor both now.

**Files:**
- Create: `fixtures/policy/fleet-fixture/fleet.toml`
- Create: `fixtures/policy/fleet-fixture/docs/agent/ownership.md`
- Create: `fixtures/policy/fleet-fixture/docs/agent/task-classes.md`
- Create: `fixtures/policy/fleet-fixture/.agents/schemas/handoff.schema.json`
- Create: `fixtures/policy/fleet-fixture/.agents/schemas/verdict.schema.json`
- Create: `fixtures/policy/fleet-fixture/.agents/roles/*.md` (all six role prompts)
- Create: `fixtures/policy/README.md`
- Modify: `tests/conftest.py:22`

**Interfaces:**
- Consumes: nothing
- Produces: `tests/conftest.py` exposes `FIXTURE_POLICY: Path` (the vendored `fleet-fixture` root) and `FIXTURE_SCHEMAS: Path` (`FIXTURE_POLICY / ".agents/schemas"`). Every later task's `cfg` fixture resolves through these.

The roles directory is vendored for the same reason as the rest, and one more: the vendored `fleet.toml` sets `roles = ".agents/roles"`, so a copy without it hands every consumer a `cfg.roles_dir` pointing at nothing. Task 5's `parse_routes` validates each route's role against the files in that directory — a strict parser with an empty vocabulary either rejects every route or accepts every string, and both are worse than the prose it is guarding against.

- [ ] **Step 1: Copy the policy layer out of the fixture repo**

```bash
cd ~/TIG/fleet
mkdir -p fixtures/policy/fleet-fixture/docs/agent
mkdir -p fixtures/policy/fleet-fixture/.agents/schemas
mkdir -p fixtures/policy/fleet-fixture/.agents/roles
cp ~/TIG/fleet-fixture/fleet.toml                      fixtures/policy/fleet-fixture/
cp ~/TIG/fleet-fixture/docs/agent/ownership.md         fixtures/policy/fleet-fixture/docs/agent/
cp ~/TIG/fleet-fixture/docs/agent/task-classes.md      fixtures/policy/fleet-fixture/docs/agent/
cp ~/TIG/fleet-fixture/.agents/schemas/*.json          fixtures/policy/fleet-fixture/.agents/schemas/
cp ~/TIG/fleet-fixture/.agents/roles/*.md              fixtures/policy/fleet-fixture/.agents/roles/
```

The six role prompts are the role vocabulary Task 5 validates against, so check they all arrived:

```bash
ls fixtures/policy/fleet-fixture/.agents/roles/ | wc -l
```
Expected: `6`

Verify the schema copy carries the conditional-field backstop (it must, as of `fleet-fixture` commit `7377f9e`):

```bash
grep -c '"if"' fixtures/policy/fleet-fixture/.agents/schemas/handoff.schema.json
```
Expected: `2`

- [ ] **Step 2: Write the capture note**

Create `fixtures/policy/README.md`:

```markdown
# Vendored policy layer

A copy of `FibonAdithya/fleet-fixture`'s policy layer, captured the same way
`fixtures/ledger/` captures gh output and for the same reason: a fixture that
silently tracks a live working tree is not a fixture.

| Path | Captured from |
|---|---|
| `fleet-fixture/fleet.toml` | `~/TIG/fleet-fixture/fleet.toml` |
| `fleet-fixture/docs/agent/*.md` | `~/TIG/fleet-fixture/docs/agent/` |
| `fleet-fixture/.agents/schemas/*.json` | `~/TIG/fleet-fixture/.agents/schemas/` |
| `fleet-fixture/.agents/roles/*.md` | `~/TIG/fleet-fixture/.agents/roles/` |

Captured at `fleet-fixture` commit `7377f9e`.

## Why this exists

`tests/conftest.py` used to read `~/TIG/fleet-fixture/.agents/schemas` directly.
That is an absolute path into another repo's *working tree*, so the suite
validated against whatever happened to be checked out there. It was not
hypothetical: `fleet#1` was merged only after committing a schema change that
had been sitting uncommitted in that tree, and until it landed the suite's 121
green tests were reading a file no commit contained.

## Re-capturing

A policy change in `fleet-fixture` does not reach this copy on its own. Re-run
the copy above deliberately, and say so in the commit message.
```

- [ ] **Step 3: Repoint conftest**

In `tests/conftest.py`, replace the `FIXTURE_SCHEMAS` block:

```python
# Vendored, not a live path into another repo's working tree. See
# fixtures/policy/README.md for why that distinction cost us a merge.
FIXTURE_POLICY = Path(__file__).resolve().parents[1] / "fixtures/policy/fleet-fixture"
FIXTURE_SCHEMAS = FIXTURE_POLICY / ".agents/schemas"
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest`
Expected: `121 passed` — unchanged. The vendored schemas are byte-identical to what the suite was reading.

- [ ] **Step 5: Prove the suite no longer depends on the sibling repo**

```bash
mv ~/TIG/fleet-fixture ~/TIG/fleet-fixture.hidden
uv run pytest
mv ~/TIG/fleet-fixture.hidden ~/TIG/fleet-fixture
```
Expected: `121 passed` with the sibling repo absent. Before this task it would error in `shutil.copytree`.

- [ ] **Step 6: Commit**

```bash
git add fixtures/policy tests/conftest.py
git commit -m "$(cat <<'EOF'
test(fixtures): vendor the policy layer instead of reading a live working tree

conftest pointed FIXTURE_SCHEMAS at ~/TIG/fleet-fixture/.agents/schemas, an
absolute path into another repo's working tree, so the suite validated against
whatever was checked out there. fleet#1 shipped 121 green tests reading a schema
file no commit contained; it went green only because an uncommitted edit
happened to be sitting in that tree.

M2's scenarios need a policy seed regardless, so the fixture has to exist.
Pointing the schema fixture at it is then one line.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Parse the `[budget]` block

`fleet.toml` already carries `per_task_usd = 2.0` and `fleet_usd = 25.0`; `load_config` reads `[repo]`, `[policy]` and `[fleet]` only. Parent §10's drain rule is a `decide()` rule with no input wired.

**Files:**
- Modify: `src/fleet/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `FleetConfig` from Task 0 (existing)
- Produces: `FleetConfig` gains `per_task_usd: float` and `fleet_usd: float`, defaulting to `2.0` and `25.0`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_budget_is_parsed(tmp_path):
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = "r"\nschemas = "s"\n[budget]\nper_task_usd = 3.5\nfleet_usd = 40.0\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.per_task_usd == 3.5
    assert cfg.fleet_usd == 40.0


def test_budget_has_defaults_when_the_block_is_absent(tmp_path):
    """The block is optional, but decide()'s drain rule is not -- a missing
    [budget] must yield a real ceiling, not None, or the drain never fires."""
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = "r"\nschemas = "s"\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.per_task_usd == 2.0
    assert cfg.fleet_usd == 25.0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -k budget -v`
Expected: FAIL — `AttributeError: 'FleetConfig' object has no attribute 'per_task_usd'`

- [ ] **Step 3: Implement**

In `src/fleet/config.py`, add two fields to the `FleetConfig` dataclass after `max_fix_passes`:

```python
    per_task_usd: float
    fleet_usd: float
```

Name the two defaults as module constants near the top of `config.py`, beside the
`DEFAULT_MAX_ATTEMPTS` import:

```python
DEFAULT_PER_TASK_USD = 2.0
DEFAULT_FLEET_USD = 25.0
```

They live here rather than in `labels.py` because a budget is not label vocabulary. The
counters are imported from `labels.py` for a reason that does not apply to dollars: the label
set is *sized* from them, so a vocabulary disagreeing with policy yields a label gh has never
heard of. Naming them is still the module's own pattern — `config.py` already imports two
defaults rather than repeating their literals, and a default written twice is a default that
can be changed once.

In `load_config`, change the unpacking line and add the two fields:

```python
    repo, policy, fleet = data["repo"], data["policy"], data.get("fleet", {})
    budget = data.get("budget", {})
```

and inside the `FleetConfig(...)` call, after `max_fix_passes=...`:

```python
        per_task_usd=float(budget.get("per_task_usd", DEFAULT_PER_TASK_USD)),
        fleet_usd=float(budget.get("fleet_usd", DEFAULT_FLEET_USD)),
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS, all config tests

- [ ] **Step 5: Commit**

```bash
git add src/fleet/config.py tests/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): parse the budget block

per_task_usd and fleet_usd have been sitting in fleet.toml since M0 with nothing
reading them. decide()'s "at 80%: drain, do not kill" rule is the first consumer.

Defaults rather than None when the block is absent: an optional block is fine,
but a missing ceiling that reads as None means the drain never fires, which is
the failure the rule exists to prevent.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: The domain types

Every type in M2 is defined here, so every disagreement between two later tasks
is a disagreement about this file. Three of them are already visible: the attempt
counter had two writers, the integration branch was a hardcoded string in
`_dispatch` while `FleetConfig` already parsed it, and a `Task` carrying a
handoff packet could not be hashed. All three are fixed at the type, once,
rather than at each call site.

**Files:**
- Create: `src/fleet/model.py`
- Test: `tests/test_model.py`

**Interfaces:**
- Consumes: nothing
- Produces: every type the rest of M2 uses. Exact definitions below — later tasks import these names verbatim.
- `Dispatch(task, role, base, attempt)` and `Release(task, reason, attempt)`: `decide()` computes the attempt number, `expand()` reads it off the intent. Field order is the order `simulate.render_intent` prints (every field but `base`).
- `Policy` carries `integration_branch`; Task 5's `load` fills it from `cfg.integration_branch` and Task 10's `_dispatch` reads it instead of a literal.
- The `Intent` union is exactly the six intents `decide()` emits, and `Effect` the four `expand()` emits. Nothing else.

- [ ] **Step 1: Write the failing test**

Create `tests/test_model.py`:

```python
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import get_args

import pytest

from fleet.model import (
    AgentState,
    Dispatch,
    Effect,
    FleetState,
    Intent,
    Observation,
    Policy,
    Release,
    Task,
)


def _policy(**kw) -> Policy:
    fields = {
        "areas": (("src/eval/**", "eval"),),
        "routes": (("patch", ("implementer", "reviewer", "integrator")),),
        "slots": 6,
        "stall_minutes": 10,
        "max_attempts": 2,
        "max_fix_passes": 1,
        "per_task_usd": 2.0,
        "fleet_usd": 25.0,
        "integration_branch": "integration",
    }
    return Policy(**{**fields, **kw})


def test_a_task_carrying_a_packet_is_still_hashable():
    """Catches dropping `hash=False` from `packet`/`verdict`: a frozen dataclass
    hashes every compared field, so a populated packet makes `hash(task)` raise
    TypeError. Hashing a packet-less task -- which is what this test used to do
    -- proves nothing, because the dict is the whole problem."""
    task = Task(
        number=1,
        title="t",
        state="review",
        area="eval",
        klass="patch",
        auto_ok=True,
        packet={"to": "review", "blocked_on": None},
        verdict={"verdict": "approved"},
    )
    assert isinstance(hash(task), int)
    assert len({task, task}) == 1


def test_a_task_is_frozen():
    """Catches removing `frozen=True`: a caller that edits what it was given
    corrupts a shared snapshot, which is the M1 defect where FakeGh handed out
    its live `labels` list."""
    task = Task(number=1, title="t", state="ready", area="eval", klass="patch", auto_ok=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.state = "claimed"


def test_task_defaults_are_the_absent_case_not_the_zero_case():
    """Catches defaulting `packet` to `{}` or a counter to 1. `decide()` reads
    `attempts` as "how many have been spent", so a default of 1 spends one."""
    task = Task(number=1, title="t", state="triage", area=None, klass=None, auto_ok=False)
    assert task.attempts == 0
    assert task.fix_passes == 0
    assert task.packet is None
    assert task.verdict is None
    assert task.blocked_on is None


def test_intents_compare_by_value_including_the_attempt():
    """Catches `eq=False`, and catches an intent that drops `attempt` from
    equality: `decide()`'s tests assert on intent equality, so two dispatches
    differing only in attempt number must not compare equal."""
    assert Dispatch(task=1, role="implementer", base="integration", attempt=1) == Dispatch(
        task=1, role="implementer", base="integration", attempt=1
    )
    assert Dispatch(task=1, role="implementer", base="integration", attempt=1) != Dispatch(
        task=1, role="implementer", base="integration", attempt=2
    )
    assert Release(task=1, reason="stalled", attempt=2) != Release(
        task=1, reason="stalled", attempt=1
    )


def test_dispatch_field_order_is_the_render_order():
    """Catches reordering `Dispatch`'s fields. `simulate.render_intent` prints
    every field but `base` in declaration order, so a scenario's
    `Dispatch(5, implementer, 1)` silently becomes a different string if the
    attempt moves ahead of the role."""
    names = [f.name for f in dataclasses.fields(Dispatch)]
    assert names == ["task", "role", "base", "attempt"]


def test_policy_route_for_returns_an_empty_tuple_for_an_unknown_class():
    """Catches `route_for` returning None: `_dispatch` reads `route[0]` after a
    falsiness check, and None passes the check but raises on the subscript."""
    policy = _policy()
    assert policy.route_for("patch") == ("implementer", "reviewer", "integrator")
    assert policy.route_for("nonexistent") == ()
    assert policy.route_for(None) == ()


def test_policy_carries_the_integration_branch():
    """Catches hardcoding `base="integration"` in `_dispatch`. A target repo
    whose integration branch is named anything else would otherwise have every
    agent started off the wrong base, and no test would notice."""
    assert _policy(integration_branch="trunk").integration_branch == "trunk"


def test_agent_state_carries_what_a_snapshot_cannot():
    """Catches dropping `task` or `cost_usd`: herdr's snapshot has no timestamp
    and no issue number -- only agent_status, state_change_seq and a worktree
    cwd. The shell supplies both missing facts; this type is where they arrive."""
    state = AgentState(task=5, status="working", change_seq=551, cost_usd=0.4)
    assert (state.task, state.cost_usd) == (5, 0.4)


def test_fleet_state_looks_an_observation_up_by_task():
    """Catches an `observation` that returns the whole `seen` tuple or raises on
    an unknown task: the first tick a task is seen has no prior observation, and
    stall detection asks for it anyway."""
    obs = Observation(change_seq=551, since=datetime(2026, 8, 17, tzinfo=timezone.utc))
    state = FleetState(seen=((5, obs),))
    assert state.observation(5) is obs
    assert state.observation(6) is None


def test_no_intent_or_effect_exists_that_nothing_emits():
    """Catches re-adding `CreateIntegrationTask` or `CreateIssue`. A union member
    no phase of `decide()` emits is either a dead branch in `expand()` or an
    unhandled member that falls through to a silent no-op. Epic fan-in is M3;
    when M3 wants these back it re-adds them together with their emitter."""
    assert {t.__name__ for t in get_args(Intent)} == {
        "Dispatch",
        "Release",
        "Escalate",
        "Unblock",
        "Advance",
        "Drain",
    }
    assert {t.__name__ for t in get_args(Effect)} == {
        "SetLabels",
        "Assign",
        "StartAgent",
        "StopAgent",
    }
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_model.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.model'`

- [ ] **Step 3: Implement**

Create `src/fleet/model.py`:

```python
"""Frozen domain types. Every collection is a tuple, so a snapshot handed to a
caller cannot be edited underneath the code that produced it -- the same defect
FakeGh.get_issue was fixed for in M1."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Task:
    """One GitHub issue, as the allocator sees it.

    `packet` and `verdict` are populated only for tasks whose state carries one,
    because `gh issue list` does not fetch comment bodies and FakeGh mirrors
    that. `None` here is a fact about the query, not about the issue.

    Those two are the one exception to the tuples-everywhere rule: a handoff
    packet is a parsed JSON document, and freezing it would mean re-typing a
    schema that already has one. They are declared `hash=False` so the exception
    costs nothing -- a frozen dataclass hashes every compared field, so without
    it `hash(Task(..., packet={...}))` raises TypeError and every set or dict
    keyed by Task breaks the moment a packet arrives.
    """

    number: int
    title: str
    state: str
    area: str | None
    klass: str | None
    auto_ok: bool
    attempts: int = 0
    fix_passes: int = 0
    assignee: str | None = None
    is_open: bool = True
    blocked_on: int | None = None
    parent_epic: int | None = None
    packet: dict | None = field(default=None, hash=False)
    verdict: dict | None = field(default=None, hash=False)


@dataclass(frozen=True)
class AgentState:
    """A herdr agent, normalized.

    `task` and `cost_usd` are supplied by the shell: the herdr snapshot carries
    neither. `change_seq` is herdr's `state_change_seq`, a monotonic counter --
    a value that has not moved since the last tick is what stall detection reads.
    """

    task: int
    status: str  # "working" | "idle" | "done" | "dirty" | "gone"
    change_seq: int
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Observation:
    """When a task's agent sequence was last seen to move."""

    change_seq: int
    since: datetime


@dataclass(frozen=True)
class FleetState:
    """The fold's carried memory. Derived and disposable: task state lives in
    labels and ownership lives in the assignee, both on GitHub. Losing this
    costs one thing -- idle timers restart, so a stall in progress is noticed up
    to stall_minutes late. That is the whole cost of a daemon restart, which is
    what keeps parent §9's promise true rather than aspirational."""

    seen: tuple[tuple[int, Observation], ...] = ()
    spent_usd: float = 0.0

    def observation(self, task: int) -> Observation | None:
        return dict(self.seen).get(task)


@dataclass(frozen=True)
class Policy:
    """The parsed policy layer. `integration_branch` has no default on purpose:
    a Policy built without it fails at construction rather than dispatching every
    agent off a branch named "integration" in a repo that calls it something
    else -- a mistake no test notices, because the fixture happens to agree."""

    areas: tuple[tuple[str, str], ...]  # (glob, area)
    routes: tuple[tuple[str, tuple[str, ...]], ...]  # (class, role sequence)
    slots: int
    stall_minutes: int
    max_attempts: int
    max_fix_passes: int
    per_task_usd: float
    fleet_usd: float
    integration_branch: str

    def route_for(self, klass: str | None) -> tuple[str, ...]:
        return dict(self.routes).get(klass or "", ())


# ---------------------------------------------------------------- intents


@dataclass(frozen=True)
class Dispatch:
    """`attempt` is the number this dispatch will stamp on the task, computed by
    `decide()` and read back by `expand()`. Carrying it on the intent is what
    keeps one writer: with the counter defaulted in `expand` and hardcoded at the
    `Dispatch` site, the two disagreed and the label never advanced past 1.

    Field order is the render order (`simulate.render_intent` prints every field
    but `base`), so a scenario asserts `Dispatch(5, implementer, 1)`."""

    task: int
    role: str
    base: str
    attempt: int


@dataclass(frozen=True)
class Release:
    task: int
    reason: str
    attempt: int


@dataclass(frozen=True)
class Escalate:
    task: int
    reason: str


@dataclass(frozen=True)
class Unblock:
    task: int


@dataclass(frozen=True)
class Advance:
    task: int
    to: str


@dataclass(frozen=True)
class Drain:
    reason: str


Intent = Dispatch | Release | Escalate | Unblock | Advance | Drain


# ---------------------------------------------------------------- effects


@dataclass(frozen=True)
class SetLabels:
    """One gh edit, carrying both halves. `commands._post` removes all eight
    state labels and then adds one; a failure between the two strands the task
    with no state label at all, invisible to every label query. One effect, one
    call."""

    task: int
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assign:
    task: int
    owner: str | None


@dataclass(frozen=True)
class StartAgent:
    task: int
    role: str
    base: str


@dataclass(frozen=True)
class StopAgent:
    task: int


Effect = SetLabels | Assign | StartAgent | StopAgent
```

There is no `CreateIntegrationTask` and no `CreateIssue`. Nothing in M2 emits
either, and an `Intent` member with no emitter is either a dead branch in
`expand()` or an unhandled union member that silently does nothing. Epic fan-in
is M3's job and M3 can re-add both dataclasses together with the code that emits
them; Task 17 records the deferral so it is not lost.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_model.py -v && uv run ruff check src/fleet/model.py tests/test_model.py && uv run ruff format --check src/fleet/model.py tests/test_model.py`
Expected: PASS, 10 tests; ruff clean; both files already formatted

- [ ] **Step 5: Commit**

```bash
git add src/fleet/model.py tests/test_model.py
git commit -m "$(cat <<'EOF'
feat(model): frozen domain types for the allocator

Tuples rather than lists inside every dataclass. FakeGh handing out its live
`labels` list was an M1 defect that passed the whole offline suite and would
have done nothing against GitHub; a mutable domain type is the same shape.

`packet` and `verdict` stay dicts -- they are parsed JSON documents with a
schema already -- but are declared hash=False, because a frozen dataclass hashes
every compared field and a populated packet would otherwise make hash(Task)
raise TypeError the first time one arrived.

Dispatch and Release carry the attempt number they stamp. With the counter
defaulted in expand() and hardcoded at the Dispatch site there were two writers
and they disagreed. Policy carries integration_branch for the same reason: it
was already parsed in FleetConfig and re-typed as a literal in _dispatch.

FleetState carries only what one snapshot cannot answer -- idle timers and the
sequence counters behind stall detection. It holds no authority, so losing it
on restart costs a late stall detection and nothing else.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `ledger.py` — the parity choke point

Every later task reads the ledger through this module, so a parse that is wrong
about the label vocabulary is wrong everywhere at once. It was: reading the state
by `fleet:` prefix rejected `fleet:auto-ok`, `fleet:attempt-N` and
`fleet:fix-pass-N` as unknown states, which is 11 of the 13 rows in the captured
ledger — every task that is dispatchable or has been tried once. The state is a
membership test against the eight state labels, not a prefix test.

**Files:**
- Create: `src/fleet/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Consumes: `Task` from Task 3; `labels.all_state_labels`, `labels.AREAS`, `labels.CLASSES`, `labels.AUTO_OK` (existing)
- Produces: `read(rows, packets=None, verdicts=None) -> list[Task]`, `read_one(row, packet=None, verdict=None) -> Task`, and `LedgerError`. Accepts both gh's label-object shape and `FakeGh`'s label-name shape.
- Reads `fixtures/ledger/issues.json` in one test (captured in M1; no task creates it).

- [ ] **Step 1: Write the failing test**

Create `tests/test_ledger.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet import ledger
from fleet.gh import FakeGh

CAPTURED = Path(__file__).resolve().parents[1] / "fixtures/ledger/issues.json"


def row(number=1, labels=(), state="OPEN", title="t", body="", assignees=()):
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "labels": list(labels),
        "assignees": list(assignees),
    }


def test_label_objects_and_label_names_parse_identically():
    """Catches a parse that indexes label dicts directly, or one that assumes
    names. gh returns [{"id":..,"name":..}]; FakeGh stores ["fleet:ready"]. This
    is the divergence fixtures/ledger/README.md warns about, and ledger.read is
    the one place that has to absorb it."""
    as_objects = row(labels=[{"id": "x", "name": "fleet:ready"}, {"id": "y", "name": "area:eval"}])
    as_names = row(labels=["fleet:ready", "area:eval"])
    assert ledger.read_one(as_objects) == ledger.read_one(as_names)


def test_state_is_read_from_the_label_not_the_issue_state():
    """Catches reading `row["state"]` as the fleet state. That field is
    OPEN/CLOSED -- the issue's openness -- and conflating the two makes every
    open task look "ready"."""
    task = ledger.read_one(row(labels=["fleet:claimed"], state="OPEN"))
    assert task.state == "claimed"
    assert task.is_open is True


def test_the_other_fleet_labels_are_not_mistaken_for_states():
    """Catches the state parse regressing to a `fleet:` prefix test. auto-ok,
    attempt-N and fix-pass-N all carry that prefix without being states, so a
    prefix test raises `unknown state: 'auto-ok'` on 11 of the 13 captured rows
    -- i.e. on every task that is dispatchable or has been tried once."""
    task = ledger.read_one(
        row(labels=["fleet:claimed", "fleet:auto-ok", "fleet:attempt-2", "fleet:fix-pass-1"])
    )
    assert (task.state, task.auto_ok, task.attempts, task.fix_passes) == ("claimed", True, 2, 1)


def test_closed_is_matched_case_insensitively():
    """Catches `row["state"] == "CLOSED"`: gh's --state flag accepts any casing
    and FakeGh mirrors that, so a literal comparison diverges from both."""
    assert ledger.read_one(row(state="CLOSED")).is_open is False
    assert ledger.read_one(row(state="closed")).is_open is False


def test_a_task_with_no_state_label_reads_as_triage():
    """Catches raising on a state-less row. _post removes all eight state labels
    then adds one; a crash between the two leaves a task label-less, and the
    allocator must see it rather than KeyError on it."""
    assert ledger.read_one(row(labels=["area:eval"])).state == "triage"


def test_two_state_labels_are_rejected_loudly():
    """Catches a parse that silently picks the first of two. A task in both
    fleet:claimed and fleet:review is a torn write, and picking one at random
    means the tick's decision depends on gh's label ordering."""
    with pytest.raises(ledger.LedgerError, match="more than one state label"):
        ledger.read_one(row(labels=["fleet:claimed", "fleet:review"]))


def test_counters_take_the_highest_label_present():
    """Catches counting labels instead of reading them, and catches taking the
    first: attempt-1 is not removed when attempt-2 is added."""
    task = ledger.read_one(row(labels=["fleet:attempt-1", "fleet:attempt-2"]))
    assert task.attempts == 2


def test_counters_are_zero_when_absent():
    """Catches a counter that defaults to 1: a task on its first dispatch has no
    attempt label yet, and `attempts=1` there spends an attempt nobody used."""
    task = ledger.read_one(row(labels=["fleet:ready"]))
    assert task.attempts == 0
    assert task.fix_passes == 0


def test_auto_ok_is_read_from_its_own_label():
    """Catches inferring auto_ok from area or class. Spec §5 ruling 1: the label
    alone gates dispatch, never the area."""
    assert ledger.read_one(row(labels=["fleet:auto-ok"])).auto_ok is True
    assert ledger.read_one(row(labels=["area:configs"])).auto_ok is False


def test_area_and_class_are_read_from_their_prefixes():
    """Catches swapping the two prefixes, which would leave every task's area
    None and disable area exclusion entirely."""
    task = ledger.read_one(row(labels=["area:configs", "class:investigation"]))
    assert task.area == "configs"
    assert task.klass == "investigation"


def test_an_unknown_area_label_is_rejected_loudly():
    """Catches dropping the AREAS check. A typo'd area silently becomes an area
    no ownership rule covers, so the task is excluded from nothing and every
    agent piles into it."""
    with pytest.raises(ledger.LedgerError, match="unknown area"):
        ledger.read_one(row(labels=["area:evla"]))


def test_two_area_labels_are_rejected_loudly():
    """Catches a parse that validates all values but returns the first. Area
    exclusion holds ONE area per task, so a task in area:eval and area:data is
    excluded from one while its agent edits both."""
    with pytest.raises(ledger.LedgerError, match="more than one area"):
        ledger.read_one(row(labels=["area:eval", "area:data"]))


def test_the_captured_real_ledger_parses():
    """The whole point of capturing it. If this ever fails, the fake and the real
    client have diverged again. The auto_ok count is the load-bearing assertion:
    a `fleet:` prefix state parse raises on exactly those 11 rows."""
    tasks = ledger.read(json.loads(CAPTURED.read_text()))
    assert len(tasks) == 13
    assert sum(1 for t in tasks if t.auto_ok) == 11
    by_number = {t.number: t for t in tasks}
    assert by_number[1].state == "ready"
    assert by_number[1].area == "eval"
    assert by_number[1].klass == "patch"
    assert by_number[1].auto_ok is True
    assert by_number[1].attempts == 0
    assert by_number[10].klass == "investigation"
    assert by_number[12].state == "review"
    assert by_number[12].is_open is False
    assert by_number[13].state == "triage"


def test_fake_gh_output_parses_the_same_way():
    """Parity: the same assertions over the fake's shape. Catches a parse that
    only ever worked against the captured file."""
    gh = FakeGh()
    gh.create_issue("t", "b", ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"])
    task = ledger.read(gh.list_issues("all"))[0]
    assert (task.state, task.area, task.klass, task.auto_ok) == ("ready", "eval", "patch", True)


def test_the_assignee_is_read_from_gh_s_object_shape():
    """Catches storing the raw assignee object. Every captured row has an empty
    `assignees`, so nothing else in the suite exercises this branch -- and
    ownership lives in the assignee, so a dict there means no task ever matches
    its owner."""
    assert ledger.read_one(row(assignees=[{"login": "octocat"}])).assignee == "octocat"
    assert ledger.read_one(row(assignees=["octocat"])).assignee == "octocat"
    assert ledger.read_one(row()).assignee is None


def test_blocked_on_comes_from_the_packet_not_a_label():
    """Catches inventing a fleet:blocked-on-N label -- one gh has never heard of,
    so applying it would fail the whole edit. The dependency lives in the handoff
    packet."""
    task = ledger.read_one(
        row(labels=["fleet:blocked"]), packet={"to": "blocked", "blocked_on": 11}
    )
    assert task.blocked_on == 11
    assert ledger.read_one(row(labels=["fleet:blocked"])).blocked_on is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ledger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.ledger'`

- [ ] **Step 3: Implement**

Create `src/fleet/ledger.py`:

```python
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
```

Note the asymmetry between `_state` and `_one_of`: the state cannot be read by
prefix because four non-state labels share `fleet:`, while `area:` and `class:`
are unambiguous prefixes. Both reject a second value rather than picking one, so
a torn label write raises here instead of making the tick's decision depend on
gh's label ordering.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ledger.py -v && uv run ruff check src/fleet/ledger.py tests/test_ledger.py && uv run ruff format --check src/fleet/ledger.py tests/test_ledger.py`
Expected: PASS, 16 tests; ruff clean; both files already formatted

- [ ] **Step 5: Commit**

```bash
git add src/fleet/ledger.py tests/test_ledger.py
git commit -m "$(cat <<'EOF'
feat(ledger): parse gh JSON into Task, from both shapes

One choke point for the divergence that produced four of M0-M1's defects: gh
returns label objects, FakeGh stores names; gh says OPEN/CLOSED, callers say
open. The parity tests assert the same properties over FakeGh output and over
the captured fixtures/ledger/issues.json.

The state is a membership test against labels.all_state_labels(), not a `fleet:`
prefix test. auto-ok, attempt-N and fix-pass-N carry that prefix without being
states, so a prefix test rejects every task that is dispatchable or has been
tried once -- 11 of the 13 captured rows.

A row with no state label reads as triage rather than raising. That is not
defensive coding -- _post removes all eight state labels then adds one, so a
crash between the two produces exactly this row, and the allocator has to see
the task rather than KeyError on it. Two state labels, or two areas, do raise:
picking one would make the decision depend on gh's label ordering.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `policy.py` — strict parsing of the policy layer

Spec §9 asks for a parser that raises rather than returning an empty mapping,
and the reason is that the permissive failure is invisible: an empty ownership
map disables area exclusion, and every task then looks dispatchable. The version
this replaces was strict about the empty table and permissive about the cell
contents, which is the wrong half. `task-classes.md`'s `investigation` route
cell is ``implementer, forbidden from writing `src/` `` — no arrow, so splitting
on the arrow yielded that whole sentence as a role name, and `_dispatch` handed
it to `StartAgent`. Captured issue #10 is `class:investigation`, so every
scenario seeded from the real ledger did it. The parser now takes each segment's
leading token and requires it to name a real role.

**Files:**
- Create: `src/fleet/policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `Policy` from Task 3; `FleetConfig` from Task 2 (for `per_task_usd` / `fleet_usd`); `FIXTURE_POLICY` from `tests/conftest.py`, added by Task 1 — **this task cannot run before Tasks 1, 2 and 3**, and the test module fails at import if it does.
- Also consumes Task 1's vendored `fixtures/policy/fleet-fixture/.agents/roles/` — `fleet.toml` points `roles` at it and route validation reads it. If that directory is missing, `load` raises `PolicyError` rather than validating nothing.
- Produces: `load(cfg: FleetConfig) -> Policy`, `parse_ownership(text: str) -> tuple[tuple[str, str], ...]`, `parse_routes(text: str, roles: frozenset[str]) -> tuple[tuple[str, tuple[str, ...]], ...]`, `known_roles(roles_dir: Path) -> frozenset[str]`, and `PolicyError`.

There is no `area_for`. It mapped a path to an area, and nothing in M2 does that:
spec §5 ruling 1 has the allocator read the area off the `area:` label and never
infer it, so the function had one caller — its own test. A path-to-area mapper is
what triage and `discover` need, both explicit spec §1 non-goals, and M3 can add
it back with a consumer attached. Exporting it now would ship a public function
whose signature nothing constrains, which is how it came to be declared
`(policy, path)` and implemented `(areas, path)` in the first place.

- [ ] **Step 1: Write the failing test**

Create `tests/test_policy.py`:

```python
from __future__ import annotations

import pytest

from conftest import FIXTURE_POLICY
from fleet.config import load_config
from fleet.policy import PolicyError, known_roles, load, parse_ownership, parse_routes

OWNERSHIP = """# Ownership map

| Glob | Area |
|---|---|
| `src/eval/**` | eval |
| `src/train/**` | train |
| `gates/**` | configs |

## Non-dispatchable paths

| Path | Why |
|---|---|
| `gates/**` | human judgement |
"""

CLASSES = """# Task classes

| Class | Route | Output | Unattended? |
|---|---|---|---|
| `patch` | implementer → reviewer → integrator | merged commit | yes |
| `spec` | planner | a spec and child issues | yes |
"""

ROLES = frozenset(
    {"implementer", "integrator", "invariant-reviewer", "planner", "reviewer", "scout"}
)


def test_ownership_reads_the_glob_table():
    """Catches reading the wrong column, and catches leaving the backticks on:
    a glob of "`src/eval/**`" matches no path fnmatch is ever given."""
    assert parse_ownership(OWNERSHIP) == (
        ("src/eval/**", "eval"),
        ("src/train/**", "train"),
        ("gates/**", "configs"),
    )


def test_ownership_ignores_the_second_table():
    """Catches a reader that takes the first table, or every table. The file
    carries a Non-dispatchable table headed Path | Why; reading it as globs maps
    `gates/**` to the area "human judgement", which then fails the AREAS check
    for a reason that names the wrong table."""
    areas = dict(parse_ownership(OWNERSHIP))
    assert areas["gates/**"] == "configs"


def test_an_empty_ownership_table_raises():
    """Catches returning (). A silently-empty map means no area exclusion, so
    every task looks dispatchable and six agents run into one area -- a failure
    that passes every test not specifically looking for it."""
    with pytest.raises(PolicyError, match="no ownership rules"):
        parse_ownership("# Ownership map\n\nNothing here.\n")


def test_an_unknown_area_in_the_ownership_map_raises():
    """Catches dropping the AREAS check: an area no label exists for excludes
    nothing, because no task can ever carry it."""
    with pytest.raises(PolicyError, match="unknown area"):
        parse_ownership("| Glob | Area |\n|---|---|\n| `src/x/**` | notanarea |\n")


def test_routes_split_on_the_arrow():
    """Catches splitting on "->" instead of U+2192, and catches not splitting at
    all: the whole cell would become a single role name."""
    assert dict(parse_routes(CLASSES, ROLES))["patch"] == (
        "implementer",
        "reviewer",
        "integrator",
    )
    assert dict(parse_routes(CLASSES, ROLES))["spec"] == ("planner",)


def test_the_real_investigation_route_keeps_only_its_leading_role():
    """The reason this module reads the vendored file and not just a synthetic
    constant. task-classes.md's investigation cell is
    `implementer, forbidden from writing `src/``, which contains no arrow -- so a
    parser that only splits on the arrow yields that entire sentence as the role,
    hands it to StartAgent, and asks herdr for a role prompt that does not exist.
    Captured issue #10 is class:investigation, so every scenario seeded from the
    real ledger dispatches it."""
    text = (FIXTURE_POLICY / "docs/agent/task-classes.md").read_text()
    routes = dict(parse_routes(text, ROLES))
    assert routes["investigation"] == ("implementer",)
    assert routes["integration"] == ("integrator",)
    assert routes["patch"] == ("implementer", "reviewer", "integrator")


def test_a_route_naming_an_unknown_role_raises():
    """Catches relaxing the role check to salvage the prose cell above. Dropping
    the prose is deliberate; dropping the check is how the garbage role name got
    through in the first place, so the strictness spec §9 asks for has to survive
    the same edit."""
    with pytest.raises(PolicyError, match="unknown role"):
        parse_routes("| Class | Route |\n|---|---|\n| `patch` | implementer → revewier |\n", ROLES)


def test_a_route_that_is_only_prose_raises():
    """Catches treating the whole cell as prose to be skipped. A class whose
    route names no role is not routable, and returning () for it would make
    `_dispatch` silently skip every task of that class forever."""
    with pytest.raises(PolicyError, match="unknown role"):
        parse_routes("| Class | Route |\n|---|---|\n| `patch` | handled by a human |\n", ROLES)


def test_an_unknown_class_raises():
    """Catches dropping the CLASSES check: a class no label exists for can never
    be carried by a task, so its route is unreachable and its absence silent."""
    with pytest.raises(PolicyError, match="unknown class"):
        parse_routes("| Class | Route |\n|---|---|\n| `path` | implementer |\n", ROLES)


def test_an_empty_class_table_raises():
    """Catches returning (): route_for would then be () for every class and
    nothing would ever be dispatched, with no error to say why."""
    with pytest.raises(PolicyError, match="no task classes"):
        parse_routes("# Task classes\n\nNothing.\n", ROLES)


def test_the_roles_directory_is_the_role_vocabulary():
    """Catches reading role names from anywhere but the target repo's own prompt
    files -- a hardcoded list here would validate routes against fleet's opinion
    of what roles exist rather than the repo's."""
    assert known_roles(FIXTURE_POLICY / ".agents/roles") == ROLES


def test_a_missing_roles_directory_raises(tmp_path):
    """Catches returning an empty set. An empty vocabulary makes every role check
    vacuous, so a typo'd role and a prose cell both validate -- exactly the
    permissive path this module exists to close."""
    with pytest.raises(PolicyError, match="no role prompts"):
        known_roles(tmp_path / "nope")


def test_load_reads_the_vendored_fixture_policy():
    """End to end against the real captured policy layer, not a synthetic one.
    Catches a `load` that drops integration_branch (dispatching every agent off
    the wrong base) or that forgets to pass the role vocabulary to parse_routes."""
    cfg = load_config(FIXTURE_POLICY)
    policy = load(cfg)
    assert policy.slots == 6
    assert policy.stall_minutes == 10
    assert policy.max_attempts == 2
    assert policy.max_fix_passes == 1
    assert policy.per_task_usd == 2.0
    assert policy.fleet_usd == 25.0
    assert policy.integration_branch == "integration"
    assert policy.route_for("patch") == ("implementer", "reviewer", "integrator")
    assert policy.route_for("investigation") == ("implementer",)
    assert dict(policy.areas)["src/eval/**"] == "eval"
    assert dict(policy.areas)["gates/**"] == "configs"
```

`from conftest import FIXTURE_POLICY` sits in the same import block as the
`fleet` imports, with no blank line between them: `pyproject.toml` sets
`src = ["src", "tests"]`, so ruff's isort treats both as first-party and splitting
them is an `I001` finding.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.policy'`

- [ ] **Step 3: Implement**

Create `src/fleet/policy.py`:

```python
"""Parse the target repo's policy layer into a Policy.

Strict on purpose. A silently-empty ownership map disables area exclusion, which
means every task looks dispatchable and the fleet runs six agents into one area
-- a failure that passes every test not specifically looking for it. Same class
as M1's fake/real divergences, and the same fix: make the permissive path
impossible rather than remembering not to take it.
"""

from __future__ import annotations

import re
from pathlib import Path

from fleet import labels as labels_module
from fleet.config import FleetConfig
from fleet.model import Policy

_CELL = re.compile(r"^\s*\|(.+)\|\s*$")
_TICKS = re.compile(r"^`(.*)`$")


class PolicyError(Exception):
    """The policy layer could not be understood."""


def _rows(text: str, header: tuple[str, ...]) -> list[list[str]]:
    """Every row of the first markdown table whose header starts with `header`.

    Matching on the header rather than taking the first table is what keeps
    ownership.md's second table -- Non-dispatchable paths, headed Path | Why --
    from being read as glob-to-area rules.
    """
    rows: list[list[str]] = []
    in_table = False
    for line in text.splitlines():
        match = _CELL.match(line)
        if not match:
            in_table = False
            continue
        cells = [c.strip() for c in match.group(1).split("|")]
        lowered = tuple(c.lower() for c in cells[: len(header)])
        if lowered == header:
            in_table = True
            rows = []
            continue
        if not in_table:
            continue
        if all(set(c) <= {"-", ":"} for c in cells if c):
            continue  # the |---|---| separator
        rows.append(cells)
    return rows


def _unticked(cell: str) -> str:
    match = _TICKS.match(cell.strip())
    return match.group(1) if match else cell.strip()


def known_roles(roles_dir: Path) -> frozenset[str]:
    """The role vocabulary: one `.md` prompt file per role, by stem.

    Raises on an empty or absent directory rather than returning an empty set,
    because an empty set makes the route check below vacuous -- every route would
    then validate, which is the permissive path this module exists to close.
    """
    names = frozenset(path.stem for path in roles_dir.glob("*.md"))
    if not names:
        raise PolicyError(f"no role prompts in {roles_dir}; every route would validate")
    return names


def parse_ownership(text: str) -> tuple[tuple[str, str], ...]:
    rows = _rows(text, ("glob", "area"))
    pairs = []
    for cells in rows:
        if len(cells) < 2:
            raise PolicyError(f"malformed ownership row: {cells!r}")
        glob, area = _unticked(cells[0]), _unticked(cells[1])
        if area not in labels_module.AREAS:
            raise PolicyError(f"unknown area {area!r} in ownership map")
        pairs.append((glob, area))
    if not pairs:
        raise PolicyError("no ownership rules found; area exclusion would be disabled")
    return tuple(pairs)


def _role(segment: str, klass: str, roles: frozenset[str]) -> str:
    """The role a route segment names, dropping any prose that follows it.

    task-classes.md's `investigation` route cell reads
    ``implementer, forbidden from writing `src/` ``. The leading token is the
    role; the clause after the comma is a constraint on that role's prompt, not
    an allocator input, and the allocator has nowhere to put it. Dropping it here
    is deliberate -- see Task 17's doc update, which records it as a known gap
    rather than leaving the reader to infer it from this function.

    What is NOT dropped is the check: the leading token must name a real role, or
    the route raises. Without it the whole cell became the "role", `_dispatch`
    handed that string to `StartAgent`, and the fleet asked herdr to start an
    agent under a role prompt that does not exist -- and captured issue #10 is
    `class:investigation`, so any scenario seeded from the real ledger does it.
    """
    role = _unticked(segment.split(",", 1)[0])
    if role not in roles:
        raise PolicyError(f"class {klass!r} routes to unknown role {role!r}")
    return role


def parse_routes(text: str, roles: frozenset[str]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    rows = _rows(text, ("class", "route"))
    routes = []
    for cells in rows:
        if len(cells) < 2:
            raise PolicyError(f"malformed task-class row: {cells!r}")
        name = _unticked(cells[0])
        if name not in labels_module.CLASSES:
            raise PolicyError(f"unknown class {name!r} in task classes")
        stages = tuple(
            _role(segment, name, roles) for segment in cells[1].split("→") if segment.strip()
        )
        if not stages:
            raise PolicyError(f"class {name!r} has no route")
        routes.append((name, stages))
    if not routes:
        raise PolicyError("no task classes found; nothing could ever be routed")
    return tuple(routes)


def load(cfg: FleetConfig) -> Policy:
    return Policy(
        areas=parse_ownership(cfg.ownership.read_text()),
        routes=parse_routes(cfg.task_classes.read_text(), known_roles(cfg.roles_dir)),
        slots=cfg.slots,
        stall_minutes=cfg.stall_minutes,
        max_attempts=cfg.max_attempts,
        max_fix_passes=cfg.max_fix_passes,
        per_task_usd=cfg.per_task_usd,
        fleet_usd=cfg.fleet_usd,
        integration_branch=cfg.integration_branch,
    )
```

The arrow is U+2192, byte-identical to the one in `task-classes.md`; an ASCII
`->` split returns the whole cell and every route becomes one long role name.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_policy.py -v && uv run ruff check src/fleet/policy.py tests/test_policy.py && uv run ruff format --check src/fleet/policy.py tests/test_policy.py`
Expected: PASS, 13 tests; ruff clean; both files already formatted

- [ ] **Step 5: Commit**

```bash
git add src/fleet/policy.py tests/test_policy.py
git commit -m "$(cat <<'EOF'
feat(policy): strict parsing of ownership and task classes

Raises rather than returning an empty mapping. An empty ownership map disables
area exclusion silently, so every task looks dispatchable and the fleet runs six
agents into one area -- a failure no test catches unless it is looking for it.

The table reader matches on header cells rather than taking the first table,
because ownership.md's second table (Non-dispatchable paths) would otherwise
parse as glob-to-area rules and map `gates/**` to the area "human judgement".

A route cell yields its leading token per arrow-separated segment, checked
against the roles the target repo actually defines. task-classes.md's
investigation route is "implementer, forbidden from writing `src/`" -- prose,
with no arrow -- and splitting on the arrow alone made that entire sentence the
role name that StartAgent would be handed. The prose constraint belongs to the
role prompt and is dropped here deliberately; the check that survives is what
keeps a genuinely misspelled role loud.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `fleet verdict`

REVIEW has no legal exit today. `verdict.schema.json` exists and nothing writes it.

Writing it is not enough on its own. A verdict that any role can post, on a task in any
state, is a second door into INTEGRATING — an implementer runs
`fleet verdict --verdict approved` on its own task and reaches the state the integrator
reads, having had nobody look at the diff. That is the identical grading-integrity hole
Task 7 closes for handoffs, and closing one door while opening the other buys nothing. So
the verdict path is role-gated and state-gated here, in the same commit that creates it.

`changes_required` is the other half of spec §5 ruling 4, and nothing in the repo
implements it: `labels.fix_pass_label` has existed since M0 with no writer. A round back
that does not increment `fleet:fix-pass-N` is a round `decide()` cannot see, so the cap never
binds and an unattended run cycles review → ready → review until the stall timer or the
budget stops it. Tasks 8-10 build the allocator's half of the same rule; the label is the
state they share, so this command writes it.

The round goes back to READY, not to CLAIMED. Spec §5 ruling 4's wording is "back to
CLAIMED", but ruling 2 says a fix pass does not consume an attempt, and CLAIMED cannot
deliver both: REAP treats a CLAIMED task with no live agent as ledger drift and releases it,
`attempt += 1`. Nothing starts an agent at the moment a verdict lands, so a task parked in
CLAIMED is drift by definition and the next tick would spend an attempt on it — two real
tries reduced to one by a review round that ruling 2 says is free. From READY, DISPATCH
picks it up with the attempt counter untouched and there is no window in which the ledger
says a task is held by nobody.

**Files:**
- Modify: `src/fleet/commands.py`
- Modify: `src/fleet/cli.py`
- Test: `tests/test_commands.py`
- Test: `tests/test_cli.py` — ruling A13 forces it. `test_an_exported_but_empty_fleet_role_falls_back_to_implementer` asserts, by name, the defaulting A13 forbids, and three neighbouring tests call `_context()` with `FLEET_ROLE` deleted. The behaviour cannot change while those stand.

**Interfaces:**
- Consumes: `validate` (existing), `render`/`parse_latest` (existing), `GhClient` (existing), `ledger.read_one` (Task 4)
- Produces: `do_verdict(cfg, gh, *, task, verdict, head_sha, reviewer, role, findings, checks_rerun=None, packet_was_sufficient=None) -> tuple[dict, str]`. `findings` is a list of `{"severity": ..., "detail": ..., "path": ...}` dicts. Returns the payload and the state the task landed in.
- Produces: `commands.VERDICT_ROLES: frozenset[str]` — the roles that may write one. Task 7 reads it for its error text.
- Produces: `commands._move_state(gh, issue, state, add=None)`, extracted from `_post`.
- Changes: `cli._context(*, role_required: bool = True)`. An unset `FLEET_ROLE` is now an error.

Why `-> tuple[dict, str]` and not the `-> dict` the plan first promised: `changes_required`
reaches two different states depending on a counter the reviewer cannot see, and "your task
went to NEEDS_HUMAN because the fix-pass cap is spent" is not something the caller should
have to re-read the ledger to discover.

Why A13's refusal lives in `cli.py` and not in the command: `cli.py:33` is the last place
that can still tell "unset" from "deliberately implementer". One line further on the
information is gone — the command layer receives a role that looks chosen and gates on a
value nobody set. `do_handoff` and `do_verdict` keep their own independent refusals so a
non-CLI caller cannot bypass the gate, but they cannot recover what `_context` threw away.

- [ ] **Step 1: Write the failing test**

In `tests/test_commands.py`, fold `do_verdict` into the EXISTING top-of-file import — do not
append a new `import` line further down the file. `make check` runs `ruff check` with `E`
selected, and a module-level import below the first statement is E402:

```python
from fleet.commands import do_block, do_escalate, do_handoff, do_verdict
```

Add the verdict base and the REVIEW fixture next to `BASE` and the `gh` fixture:

```python
VERDICT = dict(role="reviewer", reviewer="fleet-4", head_sha="a91c204")


@pytest.fixture()
def in_review(gh):
    """Issue 1 in REVIEW.

    The `gh` fixture leaves it CLAIMED, which is exactly where a verdict has to
    be refused -- so the move is made here rather than folded into `gh`, where it
    would quietly satisfy the state gate for every test in the file.
    """
    gh.remove_labels(1, ["fleet:claimed"])
    gh.add_labels(1, ["fleet:review"])
    return gh
```

Then append the tests:

```python
def test_an_approved_verdict_moves_the_task_to_integrating(cfg, in_review):
    """Catches a target table that leaves an approved task in REVIEW, and a
    _move_state that adds the new label without removing the old one -- two
    state labels on one row is what ledger.read_one refuses."""
    _, landed = do_verdict(
        cfg,
        in_review,
        task=1,
        verdict="approved",
        findings=[],
        checks_rerun="make check: pass",
        **VERDICT,
    )
    names = in_review.get_issue(1)["labels"]
    assert landed == "integrating"
    assert "fleet:integrating" in names
    assert "fleet:review" not in names
    assert "area:eval" in names


def test_changes_required_spends_a_fix_pass_on_the_way_back_to_ready(cfg, in_review):
    """Catches two mutations. A target that writes no counter: decide() reads fix
    passes off `fleet:fix-pass-N`, so a round that writes none is a round the
    allocator cannot see and the cap never binds. And a target of `claimed`
    instead of `ready`: REAP releases a CLAIMED task with no live agent as
    drift, which spends an attempt on a fix pass that ruling 2 says is free."""
    _, landed = do_verdict(
        cfg,
        in_review,
        task=1,
        verdict="changes_required",
        findings=[{"severity": "must_fix", "detail": "no test binds the fix"}],
        **VERDICT,
    )
    names = in_review.get_issue(1)["labels"]
    assert landed == "ready"
    assert "fleet:ready" in names
    assert "fleet:claimed" not in names
    assert "fleet:fix-pass-1" in names


def test_a_second_changes_required_reaches_a_human_rather_than_looping(cfg, in_review):
    """Spec §5 ruling 4's other half. With the cap spent another round back to
    READY is a second fix pass on a one-pass budget, and nothing else in the
    system stops it. Catches a target table that ignores max_fix_passes."""
    in_review.add_labels(1, ["fleet:fix-pass-1"])
    _, landed = do_verdict(
        cfg,
        in_review,
        task=1,
        verdict="changes_required",
        findings=[{"severity": "must_fix", "detail": "still no test"}],
        **VERDICT,
    )
    names = in_review.get_issue(1)["labels"]
    assert landed == "human"
    assert "fleet:human" in names
    assert "fleet:ready" not in names


def test_the_verdict_round_trips_through_the_comment(cfg, in_review):
    """Catches a render/parse pair that agrees with itself but not with the
    marker and schema id parse_latest keys on -- the shell reads the verdict back
    out of the comment, so a payload it cannot recover is a verdict decide()
    never sees."""
    do_verdict(cfg, in_review, task=1, verdict="approved", findings=[], **VERDICT)
    recovered = parse_latest(in_review.get_issue(1)["comments"], "verdict")
    assert recovered["verdict"] == "approved"
    assert recovered["head_sha"] == "a91c204"


def test_a_verdict_without_a_head_sha_is_refused(cfg, in_review):
    """A verdict that does not name the head it reviewed is not a gate. Catches
    validating after the label move, which strands the task in INTEGRATING with
    no verdict to justify it."""
    with pytest.raises(SchemaError, match="head_sha"):
        do_verdict(
            cfg, in_review, task=1, verdict="approved", findings=[], **{**VERDICT, "head_sha": ""}
        )
    assert in_review.get_issue(1)["comments"] == []
    assert "fleet:review" in in_review.get_issue(1)["labels"]


def test_changes_required_needs_at_least_one_finding(cfg, in_review):
    """The schema allows an empty findings array because an approval has nothing
    to list. Catches dropping the extra guard: "changes required" with nothing to
    change is a task the implementer cannot action, and it spends the fix pass."""
    with pytest.raises(ValueError, match="at least one finding"):
        do_verdict(cfg, in_review, task=1, verdict="changes_required", findings=[], **VERDICT)
    assert in_review.get_issue(1)["comments"] == []
    assert "fleet:fix-pass-1" not in in_review.get_issue(1)["labels"]


def test_an_unknown_verdict_value_is_refused(cfg, in_review):
    """Catches computing the target state before validating, which would map an
    unrecognised verdict onto whichever branch the code happens to fall through
    to."""
    with pytest.raises(SchemaError, match="verdict"):
        do_verdict(cfg, in_review, task=1, verdict="looks good to me", findings=[], **VERDICT)
    assert in_review.get_issue(1)["comments"] == []


def test_an_implementer_cannot_approve_its_own_task(cfg, in_review):
    """The hole this command would otherwise open: an implementer approving its
    own work reaches INTEGRATING with nobody having read the diff, and grading
    reads terminal state from labels. Catches dropping the role gate."""
    with pytest.raises(ValueError, match="implementer.*may not write a verdict"):
        do_verdict(
            cfg,
            in_review,
            task=1,
            verdict="approved",
            findings=[],
            **{**VERDICT, "role": "implementer"},
        )
    assert in_review.get_issue(1)["comments"] == []
    assert "fleet:integrating" not in in_review.get_issue(1)["labels"]


def test_the_invariant_reviewer_may_write_one_too(cfg, in_review):
    """invariant-reviewer.md ends "Emit `approved` only if all five are `ok`".
    Catches narrowing VERDICT_ROLES to {"reviewer"}, which leaves that role with
    no way to record the verdict its own prompt demands."""
    _, landed = do_verdict(
        cfg,
        in_review,
        task=1,
        verdict="approved",
        findings=[],
        **{**VERDICT, "role": "invariant-reviewer"},
    )
    assert landed == "integrating"


def test_a_verdict_on_a_task_that_never_reached_review_is_refused(cfg, gh):
    """The `gh` fixture leaves issue 1 CLAIMED. Catches dropping the state gate:
    a verdict would then walk a task nobody has reviewed straight into
    INTEGRATING, which is the hole the role gate closes from the other side."""
    with pytest.raises(ValueError, match="not review"):
        do_verdict(cfg, gh, task=1, verdict="approved", findings=[], **VERDICT)
    assert gh.get_issue(1)["comments"] == []
    assert gh.get_issue(1)["labels"] == ["fleet:claimed", "area:eval"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_commands.py -v`
Expected: FAIL at collection — `ImportError: cannot import name 'do_verdict' from 'fleet.commands'`.
The whole file fails, not just the new tests; that is what folding the import into the top
block costs, and it is cheaper than an E402.

- [ ] **Step 3: Implement**

In `src/fleet/commands.py`, add `read_one` to the imports (isort puts it between `fleet.gh`
and `fleet.packet`):

```python
from fleet.ledger import read_one
```

Add after the import block:

```python
# Only a reviewing role writes a verdict. `invariant-reviewer` is in because
# invariant-reviewer.md ends "Emit `approved` only if all five are `ok`" -- it
# emits a verdict, on a narrower question. Every other role is the same
# integrity hole ROLE_MAY_HAND_TO closes, arriving by the other door.
VERDICT_ROLES = frozenset({"reviewer", "invariant-reviewer"})
```

Replace `_post`'s three-line label move with a shared helper — the same three lines in two
places is how the two writers of a state drift apart:

```python
def _move_state(gh: GhClient, issue: int, state: str, add: list[str] | None = None) -> None:
    """Move the task's state label, carrying any counter label with it.

    Removing all eight and adding one is how the ledger keeps its "exactly one
    state label" invariant. `add` rides in the same `add_labels` call so a
    counter (`fleet:fix-pass-N`) cannot land without the state it belongs to.
    """
    gh.remove_labels(issue, labels.all_state_labels())
    gh.add_labels(issue, [labels.state_label(state), *(add or [])])


def _post(cfg: FleetConfig, gh: GhClient, packet: dict, state: str) -> dict:
    validate(cfg, "handoff", packet)
    issue = packet["task"]
    gh.comment(issue, render(packet))
    _move_state(gh, issue, state)
    return packet
```

Add the target rule next to it. It is pure and it is the only writer of this transition:
`decide()` does not model the review exit, because M2 ships no daemon and a verdict that
moved nothing until M3 would make REVIEW a dead end for the manual reviewer flow M1.5 proved.

```python
def _verdict_target(verdict: str, fix_passes: int, max_fix_passes: int) -> tuple[str, list[str]]:
    """Where a verdict sends a task, and which counter label rides along.

    Spec §5 ruling 4: approved -> INTEGRATING; `changes_required` -> another
    implementer pass with fix passes += 1, or NEEDS_HUMAN once the cap is spent.
    The counter IS the label -- `decide()` reads `fleet:fix-pass-N` back off the
    ledger, so a round that does not write one is a round the allocator cannot
    see, and an unattended run then cycles review -> ready -> review until the
    stall timer or the budget stops it.

    The fix-pass target is READY, not CLAIMED, though the spec's prose says
    "back to CLAIMED". CLAIMED means an agent holds the task, and nothing starts
    one at this moment; REAP reads a CLAIMED task with no live agent as ledger
    drift and releases it, which spends an attempt. Spec §5 ruling 2 says a fix
    pass costs no attempt, so CLAIMED here would break the rule this function
    exists to enforce. READY leaves DISPATCH to pick it up with `attempt`
    unchanged and leaves no window where a task is claimed by nobody.
    """
    if verdict == "approved":
        return "integrating", []
    if fix_passes >= max_fix_passes:
        return "human", []
    return "ready", [labels.fix_pass_label(fix_passes + 1)]
```

And add `do_verdict` after `do_escalate`:

```python
def do_verdict(
    cfg: FleetConfig,
    gh: GhClient,
    *,
    task: int,
    verdict: str,
    head_sha: str,
    reviewer: str,
    role: str,
    findings: list[dict],
    checks_rerun: str | None = None,
    packet_was_sufficient: bool | None = None,
) -> tuple[dict, str]:
    """Record a review verdict and move the task. REVIEW's only legal exit.

    Returns the payload and the state the task landed in: `changes_required`
    reaches two different states depending on the fix-pass counter, and the
    reviewer is owed the answer.
    """
    if role not in VERDICT_ROLES:
        # Without this an implementer approves its own task and reaches
        # INTEGRATING having had nobody read the diff -- the grading-integrity
        # hole ROLE_MAY_HAND_TO closes for handoffs, arriving by the other door.
        raise ValueError(
            f"role {role!r} may not write a verdict; only {', '.join(sorted(VERDICT_ROLES))} may"
        )
    if verdict == "changes_required" and not findings:
        # The schema allows an empty findings array, correctly -- an approval has
        # nothing to list. But "changes required" with nothing to change is a
        # task the implementer cannot action, and it spends the one bounded fix
        # pass. Refuse it here, where the state is known.
        raise ValueError("a changes_required verdict needs at least one finding")
    # read_one, not a local label scan: the write path and `decide()` then read
    # the same vocabulary through the same parser, so they cannot disagree about
    # what state a task is in or how many fix passes it has spent. It also
    # raises GhError for an issue that does not exist.
    current = read_one(gh.get_issue(task))
    if current.state != "review":
        # A verdict is REVIEW's exit, not a general relabelling tool. Without
        # this, `fleet verdict --verdict approved` on a task still CLAIMED walks
        # it straight past review into INTEGRATING.
        raise ValueError(
            f"task {task} is in {current.state!r}, not review; a verdict only exits REVIEW"
        )
    payload: dict = {
        "schema": "fleet.verdict.v1",
        "task": task,
        "verdict": verdict,
        "head_sha": head_sha,
        "reviewer": reviewer,
        "findings": findings,
    }
    if checks_rerun is not None:
        payload["checks_rerun"] = checks_rerun
    if packet_was_sufficient is not None:
        payload["packet_was_sufficient"] = packet_was_sufficient
    validate(cfg, "verdict", payload)
    state, extra = _verdict_target(verdict, current.fix_passes, cfg.max_fix_passes)
    gh.comment(task, render(payload))
    _move_state(gh, task, state, extra)
    return payload, state
```

In `src/fleet/cli.py`, add `do_verdict` to the `fleet.commands` import and replace
`_context` with:

```python
ROLE_UNSET = (
    "FLEET_ROLE is unset. It decides which state transitions you may write, so there is "
    "no safe default: 'implementer' hands out exactly the privilege the gate withholds, "
    "and an empty role reaches the command layer as \"unknown role ''\", which blames "
    "the agent for a variable the dispatcher never exported. Export FLEET_ROLE."
)


def _context(*, role_required: bool = True) -> tuple:
    cfg = load_config()
    gh = RealGh(cfg.repo_slug)
    # Falsy, not `is None`: a dispatcher doing `export FLEET_AGENT="$AGENT_ID"`
    # with AGENT_ID unset exports an empty string, which is not None. That would
    # skip the warning and the fallback both, then fail schema validation with
    # "from.agent: '' is too short" -- a complaint about a field the operator
    # never set. Same shape for FLEET_ROLE against the role enum.
    agent = os.environ.get("FLEET_AGENT", "").strip()
    if not agent:
        # "manual" is legitimate for a human running this by hand, which makes it
        # indistinguishable from a dispatch bug that never exported the variable.
        # Say so on stderr rather than silently misattributing an unattended run.
        click.echo("warning: FLEET_AGENT unset; attributing to 'manual'", err=True)
        agent = "manual"
    role = os.environ.get("FLEET_ROLE", "").strip()
    # An unset role is refused HERE and nowhere else, because this is the only
    # place that can still tell "unset" from "deliberately implementer". One line
    # further on the information is gone: the command layer sees a role that
    # looks chosen, and `do_handoff`/`do_verdict` gate on a value nobody set.
    # `role_required=False` is for the one command that records no role at all.
    if role_required and not role:
        raise click.ClickException(ROLE_UNSET)
    identity = {
        "role": role,
        "agent": agent,
        "run": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return cfg, gh, identity
```

Change `discover`'s one line, and say why in place:

```python
    # The only command that records no role: do_discover ignores identity, and a
    # discovery lands in TRIAGE whoever files it. Requiring FLEET_ROLE here would
    # refuse a human triaging by hand and gate nothing.
    cfg, gh, _ = _context(role_required=False)
```

Add the command after `escalate`. The option is bound to `decision`, not `verdict`: a
parameter named after its own command function shadows it inside the body.

```python
@main.command()
@click.option("--task", type=int, required=True)
@click.option(
    "--verdict", "decision", required=True, type=click.Choice(["approved", "changes_required"])
)
@click.option("--head-sha", required=True, help="The head you actually reviewed.")
@click.option("--checks-rerun", default=None, help='e.g. "make check: pass"')
@click.option(
    "--finding",
    "findings",
    multiple=True,
    help="severity:detail, e.g. must_fix:no test binds the fix",
)
@click.option("--packet-was-sufficient/--packet-was-insufficient", default=None)
def verdict(task, decision, head_sha, checks_rerun, findings, packet_was_sufficient):
    """Record a review verdict. The only legal exit from REVIEW."""
    cfg, gh, identity = _context()
    parsed = []
    for raw in findings:
        # partition, not split: a detail routinely contains a colon
        # ("src/eval/lid.py:12 clamps the wrong bound") and split would truncate
        # it. The schema's optional `path` is deliberately not a third field for
        # the same reason -- it would make that detail ambiguous.
        severity, _, detail = raw.partition(":")
        if not detail.strip():
            raise click.ClickException(f"finding {raw!r} has no detail; use severity:detail")
        parsed.append({"severity": severity.strip(), "detail": detail.strip()})
    _, landed = do_verdict(
        cfg,
        gh,
        task=task,
        verdict=decision,
        head_sha=head_sha,
        reviewer=identity["agent"],
        role=identity["role"],
        findings=parsed,
        checks_rerun=checks_rerun,
        packet_was_sufficient=packet_was_sufficient,
    )
    click.echo(f"recorded {decision} on task {task}; it is now fleet:{landed}")
```

- [ ] **Step 4: Bring `tests/test_cli.py` in line with the new contract**

`test_an_exported_but_empty_fleet_role_falls_back_to_implementer` asserts the defaulting
A13 forbids; it is the defect written down as a test, so it is replaced rather than
adjusted. Three neighbours call `_context()` with `FLEET_ROLE` deleted and now need one.

Add `import click` to the third-party block (before `pytest`; isort orders it that way).
Replace the empty-role test with these three, and add
`env.setenv("FLEET_ROLE", "implementer")` to
`test_an_exported_but_empty_fleet_agent_is_treated_as_unset`,
`test_a_real_agent_id_is_used_and_warns_about_nothing` and
`test_the_generated_run_timestamp_satisfies_the_schema`:

```python
def test_an_unset_fleet_role_is_an_error_not_an_implementer(env):
    """Defaulting to "implementer" hands out exactly the privilege the role gate
    withholds: an unconfigured run could then hand its own task to `done`, or
    fail `--to done` for a real integrator with a message about a role it never
    chose. This is the only place that can still tell unset from chosen, so it is
    the only place that can refuse."""
    env.setenv("FLEET_AGENT", "fleet-3")
    with pytest.raises(click.ClickException, match="FLEET_ROLE"):
        _context()


def test_an_exported_but_empty_fleet_role_is_an_error_too(env):
    """`export FLEET_ROLE="$ROLE"` with ROLE unset exports "", not None -- the
    same shape as the FLEET_AGENT case above, and it must not slip past the
    check."""
    env.setenv("FLEET_AGENT", "fleet-3")
    env.setenv("FLEET_ROLE", "")
    with pytest.raises(click.ClickException, match="FLEET_ROLE"):
        _context()


def test_discover_needs_no_role_at_all(env):
    """`fleet discover` records no role and gates no transition, so requiring one
    would refuse a human triaging by hand and buy nothing. Catches making the
    refusal unconditional."""
    env.setenv("FLEET_AGENT", "fleet-3")
    _, _, identity = _context(role_required=False)
    assert identity["role"] == ""
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_commands.py tests/test_cli.py -v && make check`
Expected: PASS, and `ruff check`/`ruff format --check` both clean.

- [ ] **Step 6: Commit**

```bash
git add src/fleet/commands.py src/fleet/cli.py tests/test_commands.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): fleet verdict -- the only legal exit from REVIEW

verdict.schema.json has existed since M0 with nothing writing it. The M1.5 trial
found this the hard way: agent B produced a correct approval that existed only
in its reply and would have evaporated in an unattended run. decide() cannot
advance a task out of REVIEW without one.

The command is role-gated and state-gated in the same commit that creates it. An
ungated verdict is a second door into INTEGRATING: an implementer approves its
own task, and grading -- which reads terminal state from labels -- scores it as
correct. Closing that door for handoffs while leaving it open here would buy
nothing.

changes_required now honours spec §5 ruling 4. It increments fleet:fix-pass-N on
the way back to CLAIMED, or routes to NEEDS_HUMAN once the cap is spent.
labels.fix_pass_label has had no writer until now, so the cap bound nothing and
a review loop had no end but the stall timer.

changes_required with no findings is refused here rather than in the schema. An
approval legitimately has nothing to list, so the schema cannot require a
non-empty array -- but "changes required" with nothing to change is a task the
implementer cannot action, and it spends the single bounded fix pass.

An unset FLEET_ROLE is now an error rather than a silent "implementer". cli.py's
_context is the last place that can tell unset from chosen; one line further on
the command layer gates on a value nobody set. fleet discover opts out, because
it records no role and gates no transition.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Bind a handoff's target state to the caller's role

An implementer can hand its own task to `done`, skipping review and integration. Grading reads terminal state from labels, so a self-declared `done` scores as correct.

Task 6 made a verdict REVIEW's only exit. That is only true if a reviewer cannot also leave
REVIEW by a plain handoff, so `reviewer` and `invariant-reviewer` map to the empty set here
rather than to `{claimed, integrating}`. A handoff out of REVIEW would record no verdict and
spend no fix pass, which is precisely the counter `decide()` reads to stop a review loop.
The fixture repo's `reviewer.md` still documents `fleet handoff --task <n> --to claimed`, so
the refusal names `fleet verdict` in its message: an agent following the stale prompt is
redirected rather than stranded. Updating that prompt belongs to the fixture repo, not here.

Two entries in the table are documented choices rather than derivations, and saying so is
cheaper than pretending otherwise. `planner` gets `{"review"}`: `planner.md` step 5 says
only "hand off with a packet listing the child numbers in `discovered`" and names no target
state, and `review` is the only member of the schema's `to` enum that is neither terminal
nor a self-claim — a decomposition wants a second read before its children get dispatched.
Epic fan-in is M3 (ruling A8), so nothing in M2 exercises the path. `scout` gets the empty
set, and not because a scout may do nothing: `scout.md` step 5 is "promote to `fleet:ready`",
and `ready` is not in `handoff.schema.json`'s `to` enum at all, so a scout's write is not a
handoff and has no `fleet` command in M2. Task 17 records both.

**Files:**
- Modify: `src/fleet/commands.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `do_handoff` (existing), `VERDICT_ROLES` (Task 6)
- Produces: `do_handoff` gains a required keyword `role`. `commands.ROLE_MAY_HAND_TO: dict[str, frozenset[str]]` is importable for tests and for the CLI's error text.

- [ ] **Step 1: Write the failing test**

In `tests/test_commands.py`, extend the two existing imports — again, no new module-level
import further down the file:

```python
from fleet.commands import ROLE_MAY_HAND_TO, do_block, do_escalate, do_handoff, do_verdict
from fleet.schemas import SchemaError, load_schema
```

Then append:

```python
def test_an_implementer_cannot_declare_its_own_task_done(cfg, gh):
    """Grading reads terminal state from labels, so a self-declared `done`
    scores as rubric-correct while skipping review and integration entirely.
    Catches dropping the `to not in allowed` refusal."""
    with pytest.raises(ValueError, match="implementer.*may not hand.*done"):
        do_handoff(cfg, gh, task=1, to="done", **BASE)
    assert gh.get_issue(1)["comments"] == []
    assert "fleet:done" not in gh.get_issue(1)["labels"]


def test_only_the_integrator_may_reach_done(cfg, gh):
    """The other edge of the same gate: catches a table so tight that nothing can
    write DONE and every task strands in INTEGRATING."""
    do_handoff(cfg, gh, task=1, to="done", **{**BASE, "role": "integrator"})
    assert "fleet:done" in gh.get_issue(1)["labels"]


def test_a_reviewer_may_not_hand_out_of_review_at_all(cfg, gh):
    """A verdict is REVIEW's only exit (spec §5 ruling 4). A plain handoff to
    CLAIMED leaves REVIEW with no verdict recorded and no fix pass spent, so the
    cap never binds and the review loop never ends. Catches restoring
    {"claimed", "integrating"}, which is what reviewer.md still documents."""
    with pytest.raises(ValueError, match="reviewer.*may not hand.*claimed") as excinfo:
        do_handoff(cfg, gh, task=1, to="claimed", **{**BASE, "role": "reviewer"})
    assert "fleet verdict" in str(excinfo.value)
    assert gh.get_issue(1)["comments"] == []


def test_an_unknown_role_is_refused_rather_than_permitted(cfg, gh):
    """Defaulting an unrecognised role to "allow everything" would make a typo in
    FLEET_ROLE a privilege escalation. Catches `.get(role, <every state>)`."""
    with pytest.raises(ValueError, match="unknown role"):
        do_handoff(cfg, gh, task=1, to="done", **{**BASE, "role": "superuser"})
    assert gh.get_issue(1)["comments"] == []


def test_the_role_table_agrees_with_the_handoff_schema(cfg):
    """Catches drift from handoff.schema.json in both directions: a role the
    schema knows but the table does not gets "unknown role" at runtime for a
    legitimate agent, and a target outside the `to` enum -- scout.md's `ready`,
    say -- would be permission to write a packet that cannot validate."""
    schema = load_schema(cfg, "handoff")
    roles = set(schema["properties"]["from"]["properties"]["role"]["enum"])
    targets = set(schema["properties"]["to"]["enum"])
    assert set(ROLE_MAY_HAND_TO) == roles
    assert set().union(*ROLE_MAY_HAND_TO.values()) <= targets
```

There is deliberately no `test_an_implementer_may_hand_to_review`: the existing
`test_handoff_moves_the_state_label` already makes that exact call with that exact role and
asserts strictly more about the result. A second copy would catch no mutation the first does
not.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_commands.py -v`
Expected: FAIL at collection — `ImportError: cannot import name 'ROLE_MAY_HAND_TO' from 'fleet.commands'`.

- [ ] **Step 3: Implement**

In `src/fleet/commands.py`, add the table immediately above `VERDICT_ROLES`, so the two
permission tables sit together:

```python
# Which target states each role may write with a plain handoff. An implementer
# that can reach `done` skips review and integration, and since grading reads
# terminal state from labels, a self-declared `done` scores as rubric-correct.
# This is the mechanical half of parent §4's "give every state one writer".
#
# `reviewer` and `invariant-reviewer` map to the empty set on purpose: spec §5
# ruling 4 makes a verdict REVIEW's only exit, so a reviewer that could hand to
# `claimed` would leave REVIEW without recording a verdict and without spending
# a fix pass -- the counter `decide()` reads to stop a review loop.
# `scout` is empty for a different reason: scout.md's step 5 is "promote to
# fleet:ready", and `ready` is not in handoff.schema.json's `to` enum at all, so
# no handoff can express a scout's write. `planner`'s `review` is a choice, not
# a derivation -- planner.md names no target state, and `review` is the only
# non-terminal member of that enum. Both are recorded in Task 17.
ROLE_MAY_HAND_TO: dict[str, frozenset[str]] = {
    "implementer": frozenset({"review"}),
    "reviewer": frozenset(),
    "invariant-reviewer": frozenset(),
    "planner": frozenset({"review"}),
    "integrator": frozenset({"done", "claimed"}),
    "scout": frozenset(),
}
```

Then change `do_handoff`'s signature and add the gate after the `blocked`/`human` guard,
passing `role` through to `build_handoff`:

```python
def do_handoff(cfg: FleetConfig, gh: GhClient, *, task: int, to: str, role: str, **kw) -> dict:
    if to in {"blocked", "human"}:
        # build_handoff would reject these anyway -- it requires blocked_on for
        # BLOCKED and question+considered for HUMAN -- but it can only say which
        # field is missing. Name the command instead: the caller's mistake is not
        # a forgotten argument, it is the wrong entry point.
        #
        # Checked before the role gate deliberately: every role may block or
        # escalate, and "use do_block" is the more actionable message.
        verb = "do_block" if to == "blocked" else "do_escalate"
        raise ValueError(f"use {verb} to reach {to!r}, not a plain handoff")
    allowed = ROLE_MAY_HAND_TO.get(role)
    if allowed is None:
        # Not a default-allow: a typo in FLEET_ROLE would otherwise be a
        # privilege escalation rather than an error.
        raise ValueError(f"unknown role {role!r}; cannot check what it may hand to")
    if to not in allowed:
        permitted = ", ".join(sorted(allowed)) or "nothing"
        hint = " -- use `fleet verdict`" if role in VERDICT_ROLES else ""
        raise ValueError(f"role {role!r} may not hand to {to!r}; it may hand to {permitted}{hint}")
    gh.get_issue(task)
    return _post(cfg, gh, build_handoff(task=task, to=to, role=role, **kw), to)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_commands.py tests/test_cli.py -v && make check`
Expected: PASS. `role` already arrives via `**identity` from `cli._context`, so the CLI needs
no change — and after Task 6 that role is one the operator actually set.

- [ ] **Step 5: Commit**

```bash
git add src/fleet/commands.py tests/test_commands.py
git commit -m "$(cat <<'EOF'
fix(commands): bind a handoff's target state to the caller's role

An implementer could hand its own task to `done`, skipping review and
integration. The grader reads terminal state from labels, so that scored as
rubric-correct -- an agent could pass the bar by relabelling rather than by
working.

A reviewer may hand to nothing at all. Spec §5 ruling 4 makes a verdict REVIEW's
only exit, and a plain handoff out of REVIEW records no verdict and spends no
fix pass -- so the cap the allocator reads never binds. The refusal names
`fleet verdict`, because the fixture repo's reviewer.md still documents
`--to claimed`.

An unknown role raises rather than defaulting to permissive, so a typo in
FLEET_ROLE is an error and not a privilege escalation.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: `decide()` — the REAP phase

**Files:**
- Create: `src/fleet/decide.py`
- Test: `tests/test_decide_reap.py`

**Interfaces:**
- Consumes: everything from Tasks 3–5 — including the three-field `Release(task, reason, attempt)` of ruling A1.
- Produces: `decide(prev: FleetState, ledger: list[Task], policy: Policy, agents: list[AgentState], now: datetime) -> tuple[list[Intent], FleetState]`. This signature is final; Tasks 9 and 10 extend the body only.
- Defines `POLICY` in `tests/test_decide_reap.py`; Tasks 9, 10 and 11 import it from there.

REAP does **not** model the REVIEW exit, and that is a deliberate decision worth
recording rather than an omission. Spec §5 ruling 4 is honoured by `fleet
verdict` (Task 6), which performs the transition in the same command that
records the verdict: approved → `fleet:integrating`; `changes_required` under
the cap → `fleet:ready` plus `fleet:fix-pass-N`; at the cap → `fleet:human`.
M2 ships no daemon — an explicit spec §1 non-goal — so a review exit owned by
`decide()` would move nothing at all until M3 runs a tick, and REVIEW would be a
dead end for exactly the manual reviewer flow M1.5 proved. Routing through the
CLI keeps the ledger self-consistent the moment the reviewer's command returns,
with no drift window.

Ruling 2's two counters survive that placement intact. A fix pass returns the
task to READY, where DISPATCH picks it up with `attempt=max(task.attempts, 1)` —
the number it already carries — so a round of review comments costs no attempt.
Returning it to CLAIMED instead is the one option that would be wrong: nothing
starts an agent at that moment, so the next tick would read it as ledger drift
and release it, spending the attempt ruling 2 forbids spending.
`fleet:fix-pass-N` therefore has exactly one writer, `fleet verdict`, and
`decide()` neither reads nor writes it — the same one-writer discipline ruling
A1 applies to attempts. The cost, recorded so a future daemon can find it: the
transition lives in the CLI rather than in the pure core, so M3 has to move it
if it wants to own it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_decide_reap.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fleet.decide import decide
from fleet.model import (
    Advance,
    AgentState,
    Escalate,
    FleetState,
    Observation,
    Policy,
    Release,
    Task,
)

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)

POLICY = Policy(
    areas=(("src/eval/**", "eval"),),
    routes=(("patch", ("implementer", "reviewer", "integrator")),),
    integration_branch="integration",
    slots=6,
    stall_minutes=10,
    max_attempts=2,
    max_fix_passes=1,
    per_task_usd=2.0,
    fleet_usd=25.0,
)


def claimed(number=1, attempts=1, **kw):
    return Task(
        number=number,
        title="t",
        state="claimed",
        area="eval",
        klass="patch",
        auto_ok=True,
        attempts=attempts,
        **kw,
    )


def only(intents, kind):
    return [i for i in intents if isinstance(i, kind)]


def test_a_clean_exit_advances_to_the_packets_target():
    """Catches a REAP that ignores the packet and releases every finished agent."""
    task = claimed(packet={"to": "review", "head_sha": "a91c204"})
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Advance(task=1, to="review") in intents


def test_a_clean_exit_with_no_packet_waits_instead_of_failing():
    """Spec §5: `packet is None` is a fact about the query -- `gh issue list`
    does not fetch comment bodies, so the shell fetches packets for the states
    that carry one and the next tick advances. Catches the invented `no_packet`
    failure reason, which released a healthy task and escalated it in two ticks."""
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(FleetState(), [claimed()], POLICY, agents, T0)
    assert intents == []


def test_a_packet_with_no_target_escalates_rather_than_raising():
    """Catches `task.packet["to"]`, an unguarded index that raises KeyError out
    of the pure core and takes the whole tick with it."""
    task = claimed(packet={"head_sha": "a91c204"})
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Escalate(task=1, reason="packet_without_target") in intents


def test_a_dirty_exit_releases_and_names_the_next_attempt():
    """Catches a Release that carries no attempt number, which is how the
    counter ended up with two writers disagreeing about it."""
    agents = [AgentState(task=1, status="dirty", change_seq=10)]
    intents, _ = decide(FleetState(), [claimed()], POLICY, agents, T0)
    assert Release(task=1, reason="dirty_exit", attempt=2) in intents


def test_an_agent_whose_sequence_has_not_moved_for_the_threshold_is_stalled():
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="working", change_seq=10)]
    intents, _ = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=10))
    assert Release(task=1, reason="stalled", attempt=2) in intents


def test_an_agent_still_moving_is_not_stalled():
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="working", change_seq=11)]
    intents, state = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=10))
    assert only(intents, Release) == []
    assert state.observation(1).change_seq == 11
    assert state.observation(1).since == T0 + timedelta(minutes=10)


def test_an_agent_just_under_the_threshold_is_not_stalled():
    """Catches `>` flipped to `>=` on the wrong side of the comparison."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="working", change_seq=10)]
    intents, _ = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=9, seconds=59))
    assert only(intents, Release) == []


def test_an_agent_absent_for_one_tick_does_not_reset_its_stall_timer():
    """Catches an _observe that rebuilds `seen` from this tick's agents alone. A
    snapshot that momentarily omits an agent would restart its idle clock, and a
    stall that flickers would never be detected at all."""
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    _, mid = decide(prev, [claimed()], POLICY, [], T0 + timedelta(minutes=5))
    assert mid.observation(1) == Observation(change_seq=10, since=T0)
    agents = [AgentState(task=1, status="working", change_seq=10)]
    intents, _ = decide(mid, [claimed()], POLICY, agents, T0 + timedelta(minutes=10))
    assert Release(task=1, reason="stalled", attempt=2) in intents


def test_a_second_failure_escalates_rather_than_making_a_third_attempt():
    """Parent §4's attempt cap. Without it an unattended run spends the night
    and a large token budget re-failing one task. Catches a cap checked after
    the release rather than before it."""
    agents = [AgentState(task=1, status="dirty", change_seq=10)]
    intents, _ = decide(FleetState(), [claimed(attempts=2)], POLICY, agents, T0)
    assert Escalate(task=1, reason="attempts_exhausted") in intents
    assert only(intents, Release) == []


def test_a_claimed_task_with_no_live_agent_is_released_even_at_the_attempt_cap():
    """Ledger drift; spec §5 states this row unconditionally. Catches applying
    the attempt cap to it, which burns a task to NEEDS_HUMAN for a daemon
    restart -- the one event FleetState is disposable in order to survive."""
    intents, _ = decide(FleetState(), [claimed(attempts=2)], POLICY, [], T0)
    assert Release(task=1, reason="no_agent", attempt=3) in intents
    assert only(intents, Escalate) == []


def test_over_per_task_budget_releases():
    agents = [AgentState(task=1, status="working", change_seq=10, cost_usd=2.5)]
    intents, _ = decide(FleetState(), [claimed()], POLICY, agents, T0)
    assert Release(task=1, reason="over_budget", attempt=2) in intents


def test_a_finished_agent_over_budget_still_advances_its_packet():
    """Status first, cost second. Catches the reverse order, which discards a
    completed packet because the agent ended a cent over the ceiling -- paying
    for the work and then throwing it away."""
    task = claimed(packet={"to": "review", "head_sha": "a91c204"})
    agents = [AgentState(task=1, status="done", change_seq=10, cost_usd=2.5)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Advance(task=1, to="review") in intents
    assert only(intents, Release) == []


def test_a_task_that_is_not_claimed_is_not_reaped():
    ready = Task(number=1, title="t", state="ready", area="eval", klass="patch", auto_ok=True)
    intents, _ = decide(FleetState(), [ready], POLICY, [], T0)
    assert only(intents, Release) == []


def test_only_a_reaped_agents_cost_reaches_the_running_total():
    """`AgentState.cost_usd` is cumulative per agent, so re-adding every live
    agent every tick re-adds the same dollars. Catches exactly that: six agents
    at $0.50 would drain a $25 fleet in seven ticks of a night that spent $3."""
    agents = [AgentState(task=1, status="working", change_seq=10, cost_usd=0.5)]
    _, state = decide(FleetState(), [claimed()], POLICY, agents, T0)
    assert state.spent_usd == 0.0


def test_a_reaped_agents_final_cost_reaches_the_running_total():
    """Catches the opposite mutation -- never accumulating at all, so the fleet
    budget is never spent and the drain never fires."""
    agents = [AgentState(task=1, status="dirty", change_seq=10, cost_usd=0.5)]
    _, state = decide(FleetState(spent_usd=1.0), [claimed()], POLICY, agents, T0)
    assert state.spent_usd == 1.5


def test_state_is_derived_and_carries_no_authority():
    """FleetState holds observations and spend, nothing else. Anything that
    looks like task state or ownership belongs in the ledger."""
    _, state = decide(FleetState(), [claimed()], POLICY, [], T0)
    assert set(vars(state)) == {"seen", "spent_usd"}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_decide_reap.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.decide'`

- [ ] **Step 3: Implement**

Create `src/fleet/decide.py`:

```python
"""The allocator's pure core.

A fold, not a stateless function. Parent §11 writes the signature as
decide(ledger, policy, agent_states), but two inputs are in none of those: the
herdr snapshot carries no timestamp anywhere and no issue number, only monotonic
counters and a worktree path. Threading the derived memory explicitly is what
puts the attempt counter and the stall timer under test instead of leaving them
in shell bookkeeping nothing reaches.

No clock, no I/O, no network. Time enters as `now`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fleet.model import (
    Advance,
    AgentState,
    Escalate,
    FleetState,
    Intent,
    Observation,
    Policy,
    Release,
    Task,
)

# Intents whose expansion stops the agent, so that agent's cost is final.
_REAPING = (Advance, Release, Escalate)


def _observe(prev: FleetState, agents: list[AgentState], now: datetime) -> dict[int, Observation]:
    """Advance a task's observation only when its sequence actually moved.

    Prior observations are carried forward rather than rebuilt from this tick's
    agents. A snapshot that momentarily omits an agent -- a herdr query racing a
    pane restart -- would otherwise reset that task's stall timer to zero, and a
    stall that keeps flickering is never detected at all.
    """
    out = dict(prev.seen)
    for agent in agents:
        old = out.get(agent.task)
        if old is None or old.change_seq != agent.change_seq:
            out[agent.task] = Observation(change_seq=agent.change_seq, since=now)
    return out


def _fail(task: Task, reason: str, policy: Policy) -> Intent:
    """One more attempt, or a human. The cap is checked before the release, not
    after: releasing first and counting later is the shape that produces a third
    attempt, which is one of the four hard fails."""
    attempt = task.attempts + 1
    if attempt > policy.max_attempts:
        return Escalate(task=task.number, reason="attempts_exhausted")
    return Release(task=task.number, reason=reason, attempt=attempt)


def _advance(task: Task) -> list[Intent]:
    """A clean exit, routed by its packet.

    No packet at all is not a failure. `gh issue list` does not fetch comment
    bodies, so the shell fetches packets for the states that carry one and the
    next tick advances it; spec §5 calls `packet is None` a fact about the query.
    Releasing here would burn an attempt for a healthy task and escalate it in
    two ticks.
    """
    if task.packet is None:
        return []
    target = task.packet.get("to")
    if not target:
        # A packet the handoff schema would have rejected. Escalating names it;
        # an unguarded task.packet["to"] would raise KeyError out of the pure
        # core instead, taking the whole tick down with it.
        return [Escalate(task=task.number, reason="packet_without_target")]
    return [Advance(task=task.number, to=target)]


def _stalled(
    task: Task, observations: dict[int, Observation], policy: Policy, now: datetime
) -> bool:
    observation = observations.get(task.number)
    if observation is None:
        return False
    return now - observation.since >= timedelta(minutes=policy.stall_minutes)


def _reap(
    ledger: list[Task],
    policy: Policy,
    agents: list[AgentState],
    observations: dict[int, Observation],
    now: datetime,
) -> list[Intent]:
    """Status first, cost second, per spec §5's table read in order."""
    by_task = {a.task: a for a in agents}
    intents: list[Intent] = []
    for task in ledger:
        if task.state != "claimed":
            continue
        agent = by_task.get(task.number)
        if agent is None:
            # Ledger drift, and spec §5's table states this row unconditionally:
            # herdr's state is the tiebreaker. Escalating at the attempt cap
            # instead would burn a task to NEEDS_HUMAN for a daemon restart --
            # the one event FleetState is disposable precisely to survive.
            intents.append(Release(task=task.number, reason="no_agent", attempt=task.attempts + 1))
            continue
        if agent.status == "done":
            intents += _advance(task)
            continue
        if agent.status == "dirty":
            intents.append(_fail(task, "dirty_exit", policy))
            continue
        if _stalled(task, observations, policy, now):
            intents.append(_fail(task, "stalled", policy))
            continue
        # Cost is checked only for a live agent. A finished agent a cent over
        # budget still advances its packet rather than discarding work already
        # paid for.
        if agent.cost_usd > policy.per_task_usd:
            intents.append(_fail(task, "over_budget", policy))
    return intents


def _final_cost(intents: list[Intent], agents: list[AgentState]) -> float:
    """Only a reaped agent's cost is final, so only a reaped agent's cost lands
    in the running total. `AgentState.cost_usd` is cumulative per agent: adding
    every live agent every tick re-adds the same dollars, and six agents at
    $0.50 drain a $25 fleet in seven ticks of a night that spent three."""
    stopped = {i.task for i in intents if isinstance(i, _REAPING)}
    return sum(a.cost_usd for a in agents if a.task in stopped)


def decide(
    prev: FleetState,
    ledger: list[Task],
    policy: Policy,
    agents: list[AgentState],
    now: datetime,
) -> tuple[list[Intent], FleetState]:
    observations = _observe(prev, agents, now)
    intents: list[Intent] = []
    intents += _reap(ledger, policy, agents, observations, now)
    state = FleetState(
        seen=tuple(sorted(observations.items())),
        spent_usd=prev.spent_usd + _final_cost(intents, agents),
    )
    return intents, state
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_decide_reap.py -v && uv run ruff check src/fleet/decide.py tests/test_decide_reap.py && uv run ruff format --check src/fleet/decide.py tests/test_decide_reap.py`
Expected: PASS, 16 tests, and ruff clean on both files

- [ ] **Step 5: Commit**

```bash
git add src/fleet/decide.py tests/test_decide_reap.py
git commit -m "$(cat <<'EOF'
feat(decide): the REAP phase

Stall detection reads a monotonic counter, not a clock: herdr's snapshot has no
timestamp under any key, only state_change_seq and revision. An observation
advances its `since` only when the sequence actually moves, and observations
carry forward across ticks -- rebuilding them from one snapshot's agents resets
the idle clock of any agent that momentarily vanishes, so a flickering stall is
never detected at all.

REVIEW exits only on a verdict (spec §5 ruling 4), and `fleet verdict` performs
that transition rather than decide(). M2 ships no daemon, so a review exit owned
by the pure core would move nothing until M3 runs a tick, and REVIEW would be a
dead end for the manual reviewer flow M1.5 proved. REAP therefore emits nothing
for a task in REVIEW: it is simply live, holding its area and its slot until the
reviewer's command moves it.

A fix pass returns the task to READY and DISPATCH picks it up on the attempt
number it already carries. Two counters, never one (ruling 2): attempts count
dispatch failures, fix passes count review rounds, and spending an attempt on a
round of review comments is the difference between two real tries and one.

A missing packet emits nothing rather than a failure. `gh issue list` does not
fetch comment bodies, so `packet is None` is a fact about the query; releasing
for it would escalate a healthy task in two ticks.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: `decide()` — the UNBLOCK phase

**Files:**
- Modify: `src/fleet/decide.py`
- Test: `tests/test_decide_unblock.py`

**Interfaces:**
- Consumes: `decide` from Task 8; `POLICY` from `tests/test_decide_reap.py`
- Produces: `decide` additionally emits `Unblock(task)` intents.

`blocked_on` is read off the packet a BLOCKED task carries — the handoff schema
requires `blocked_on` when `to` is `blocked` and forbids it everywhere else — so
this phase is only reachable in a scenario that seeds packets (ruling A11).

- [ ] **Step 1: Write the failing test**

Create `tests/test_decide_unblock.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from fleet.decide import decide
from fleet.model import Escalate, FleetState, Task, Unblock
from test_decide_reap import POLICY

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)


def blocked(number=4, on=11):
    return Task(
        number=number,
        title="t",
        state="blocked",
        area="models",
        klass="patch",
        auto_ok=True,
        blocked_on=on,
        packet={"to": "blocked", "blocked_on": on},
    )


def done(number=11):
    return Task(
        number=number,
        title="t",
        state="done",
        area="data",
        klass="patch",
        auto_ok=True,
    )


def test_a_blocked_task_whose_dependency_is_done_is_requeued():
    """Harness §4 issue 4: requeued unattended, no human. One of the four hard
    fails, and the mutation this catches is the phase being absent entirely."""
    intents, _ = decide(FleetState(), [blocked(), done()], POLICY, [], T0)
    assert Unblock(task=4) in intents


def test_a_blocked_task_whose_dependency_is_open_stays_blocked():
    """Catches an unblock that fires on the existence of a dependency rather
    than on its state, which requeues every blocked task on the first tick."""
    waiting = Task(
        number=11,
        title="t",
        state="claimed",
        area="data",
        klass="patch",
        auto_ok=True,
    )
    intents, _ = decide(FleetState(), [blocked(), waiting], POLICY, [], T0)
    assert Unblock(task=4) not in intents


def test_a_dependency_that_is_not_in_the_ledger_does_not_unblock():
    """A missing dependency is a reason to leave the task blocked, not to assume
    it finished. Catches an implementation that reads DONE as "not present among
    the open tasks", which requeues work whose prerequisite never landed."""
    intents, _ = decide(FleetState(), [blocked(on=99)], POLICY, [], T0)
    assert Unblock(task=4) not in intents


def test_a_blocked_task_with_no_dependency_recorded_does_not_unblock():
    """Catches the tolerant variant, `blocked_on is None or blocked_on in done`,
    which requeues a task nobody ever recorded a prerequisite for."""
    orphan = Task(
        number=4,
        title="t",
        state="blocked",
        area="models",
        klass="patch",
        auto_ok=True,
    )
    intents, _ = decide(FleetState(), [orphan], POLICY, [], T0)
    assert Unblock(task=4) not in intents


def test_unblocking_wakes_no_human():
    """block and escalate are deliberately distinct. Catches an unblock
    implemented as an escalation: a morning queue would fill with tasks that
    could have resolved themselves at 3am."""
    intents, _ = decide(FleetState(), [blocked(), done()], POLICY, [], T0)
    assert not [i for i in intents if isinstance(i, Escalate)]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_decide_unblock.py -v`
Expected: FAIL — `Unblock(task=4) not in intents`

- [ ] **Step 3: Implement**

In `src/fleet/decide.py`, add `Unblock` to the `fleet.model` import (after
`Task`, which is where isort puts it), then add this function immediately after
`_reap`:

```python
def _unblock(ledger: list[Task]) -> list[Intent]:
    """BLOCKED tasks whose dependency reached DONE. No human is involved -- that
    is the entire reason `block` is kept distinct from `escalate`."""
    done = {t.number for t in ledger if t.state == "done"}
    return [
        Unblock(task=t.number)
        for t in ledger
        # A dependency we cannot see stays blocked. Treating an unrecorded or
        # absent `blocked_on` as satisfied requeues work whose prerequisite
        # never landed.
        if t.state == "blocked" and t.blocked_on is not None and t.blocked_on in done
    ]
```

and in `decide`, on the line after `intents += _reap(...)`:

```python
    intents += _unblock(ledger)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_decide_reap.py tests/test_decide_unblock.py -v && uv run ruff check src/fleet/decide.py tests/test_decide_unblock.py`
Expected: PASS, 21 tests (16 + 5), and ruff clean

- [ ] **Step 5: Commit**

```bash
git add src/fleet/decide.py tests/test_decide_unblock.py
git commit -m "$(cat <<'EOF'
feat(decide): the UNBLOCK phase

A blocked task whose dependency reached DONE is requeued with no human involved.
Harness §7 makes this one of the four hard fails: issue 4 must self-resolve
overnight.

A dependency missing from the ledger leaves the task blocked rather than
unblocking it. Assuming a task we cannot see has finished would requeue work
whose prerequisite never landed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: `decide()` — the DISPATCH phase

**Files:**
- Modify: `src/fleet/decide.py`
- Test: `tests/test_decide_dispatch.py`

**Interfaces:**
- Consumes: `decide` from Task 9; `Policy.integration_branch` from Task 5
- Produces: `decide` additionally emits `Dispatch(task, role, base, attempt)` and `Drain(reason)`.

`agents` is a real parameter of this phase, not decoration: the drain threshold
is `prev.spent_usd + what the live agents have spent so far`. `spent_usd` holds
the final cost of agents already reaped (ruling A5), so without the live half
the fleet would keep dispatching while six agents burn the rest of the budget —
and with the plan's old `spent_usd + sum(every live agent)` accumulation it
would drain many ticks early instead, because `AgentState.cost_usd` is
cumulative and the same dollars get re-added every tick. Slots and areas still
come off the ledger's live tasks, which is what the executor's agents are a
lagging view of.

- [ ] **Step 1: Write the failing test**

Create `tests/test_decide_dispatch.py`:

```python
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from fleet.decide import decide
from fleet.model import AgentState, Dispatch, Drain, FleetState, Release, Task
from test_decide_reap import POLICY

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)


def ready(number, area="eval", klass="patch", auto_ok=True, attempts=0):
    return Task(
        number=number,
        title="t",
        state="ready",
        area=area,
        klass=klass,
        auto_ok=auto_ok,
        attempts=attempts,
    )


def live(number=1, area="eval", klass="patch"):
    return Task(
        number=number,
        title="t",
        state="claimed",
        area=area,
        klass=klass,
        auto_ok=True,
    )


def dispatched(intents):
    return sorted(i.task for i in intents if isinstance(i, Dispatch))


def test_a_ready_auto_ok_task_is_dispatched_to_the_first_role_in_its_route():
    intents, _ = decide(FleetState(), [ready(1)], POLICY, [], T0)
    assert Dispatch(task=1, role="implementer", base="integration", attempt=1) in intents


def test_dispatch_bases_the_agent_on_the_policys_integration_branch():
    """Catches a hardcoded "integration". A repo whose integration branch is
    named anything else would have every agent branched off the wrong base, and
    the fixture's own fleet.toml is the only reason the literal looks right."""
    policy = dataclasses.replace(POLICY, integration_branch="trunk")
    intents, _ = decide(FleetState(), [ready(1)], policy, [], T0)
    assert Dispatch(task=1, role="implementer", base="trunk", attempt=1) in intents


def test_a_re_dispatched_task_keeps_the_attempt_number_it_already_carries():
    """One writer for the counter: decide() computes the number and expand()
    writes it. Catches `attempt=task.attempts + 1`, which counts a dispatch as a
    failure and reaches the cap after one real try."""
    intents, _ = decide(FleetState(), [ready(1, attempts=2)], POLICY, [], T0)
    assert Dispatch(task=1, role="implementer", base="integration", attempt=2) in intents


def test_two_ready_tasks_in_one_area_yield_exactly_one_dispatch():
    """Parent §11's first case, and harness §4's issues 2 and 3."""
    intents, _ = decide(FleetState(), [ready(2, "train"), ready(3, "train")], POLICY, [], T0)
    assert len(dispatched(intents)) == 1


def test_two_ready_tasks_in_different_areas_both_dispatch():
    """Catches an exclusion that locks the whole fleet rather than one area."""
    intents, _ = decide(FleetState(), [ready(1, "eval"), ready(2, "train")], POLICY, [], T0)
    assert dispatched(intents) == [1, 2]


def test_an_area_already_held_by_a_claimed_task_is_excluded():
    agents = [AgentState(task=1, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [live(), ready(8, "eval")], POLICY, agents, T0)
    assert dispatched(intents) == []


def test_an_area_held_by_a_task_in_review_is_excluded():
    """REVIEW is live: the task keeps its area until `fleet verdict` moves it
    out. Catches a live set of CLAIMED alone, which puts a second agent into an
    area a reviewer is still reading."""
    reviewing = Task(
        number=1,
        title="t",
        state="review",
        area="eval",
        klass="patch",
        auto_ok=True,
    )
    intents, _ = decide(FleetState(), [reviewing, ready(8, "eval")], POLICY, [], T0)
    assert dispatched(intents) == []


def test_a_task_without_auto_ok_is_never_dispatched():
    intents, _ = decide(FleetState(), [ready(6, "configs", auto_ok=False)], POLICY, [], T0)
    assert dispatched(intents) == []


def test_auto_ok_is_read_from_the_label_and_never_inferred_from_area():
    """m2-inputs §2: ownership.md maps both configs/** and gates/** to
    area:configs, and gates/** is separately non-dispatchable. Issue 6 is
    legitimately area:configs AND auto-ok, because its criteria forbid touching
    gates/. An allocator gating on area would refuse it -- one of the four hard
    fails -- or, inverted, permit a gates/ edit."""
    intents, _ = decide(FleetState(), [ready(6, "configs", auto_ok=True)], POLICY, [], T0)
    assert dispatched(intents) == [6]


def test_dispatch_stops_entirely_while_an_integration_task_is_live():
    """Harness amendment 2: the integrator touches every area by definition, so
    per-area exclusion does not cover it. A global exclusive lock does."""
    integrating = live(number=20, area=None, klass="integration")
    agents = [AgentState(task=20, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [integrating, ready(1, "eval")], POLICY, agents, T0)
    assert dispatched(intents) == []


def test_slots_bound_the_number_of_dispatches():
    small = dataclasses.replace(POLICY, slots=2)
    backlog = [ready(1, "eval"), ready(2, "train"), ready(3, "models"), ready(4, "data")]
    intents, _ = decide(FleetState(), backlog, small, [], T0)
    assert len(dispatched(intents)) == 2


def test_a_live_task_consumes_a_slot_even_though_it_is_not_ready():
    """Slots are counted off the ledger's live tasks, not off the dispatches
    this tick. Catches `free = policy.slots`, which would run one agent per slot
    on top of everything already running."""
    small = dataclasses.replace(POLICY, slots=1)
    agents = [AgentState(task=1, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [live(), ready(2, "train")], small, agents, T0)
    assert dispatched(intents) == []


def test_at_eighty_percent_of_budget_the_fleet_drains_rather_than_killing():
    """Parent §10: drain, do not kill. In-flight tasks finish; nothing new
    starts. Killing at the ceiling throws away work already paid for."""
    spent = FleetState(spent_usd=20.0)  # 80% of 25.0
    intents, _ = decide(spent, [ready(1, "eval")], POLICY, [], T0)
    assert dispatched(intents) == []
    assert Drain(reason="budget") in intents


def test_money_committed_to_live_agents_counts_toward_the_drain():
    """The running total holds only the cost of agents already reaped, so the
    threshold has to add what the live ones have spent so far. Catches a drain
    test that reads spent_usd alone and keeps dispatching while six agents burn
    the rest of the fleet budget."""
    agents = [AgentState(task=1, status="working", change_seq=1, cost_usd=1.5)]
    intents, _ = decide(FleetState(spent_usd=19.0), [live(), ready(2, "train")], POLICY, agents, T0)
    assert dispatched(intents) == []
    assert Drain(reason="budget") in intents


def test_below_the_drain_threshold_dispatch_continues():
    """Catches a threshold that fires early -- e.g. one that re-adds a
    cumulative per-agent total every tick."""
    intents, _ = decide(FleetState(spent_usd=19.9), [ready(1, "eval")], POLICY, [], T0)
    assert dispatched(intents) == [1]


def test_draining_does_not_stop_reaping_or_unblocking():
    """Drain stops dispatch only. A stalled agent must still be released, or the
    fleet drains into a state nothing cleans up."""
    intents, _ = decide(FleetState(spent_usd=25.0), [live()], POLICY, [], T0)
    assert Release(task=1, reason="no_agent", attempt=1) in intents


def test_a_task_in_triage_is_never_dispatched():
    """Discoveries land in TRIAGE. An agent that could both file and claim work
    has authorized itself."""
    triage = Task(number=13, title="t", state="triage", area="data", klass=None, auto_ok=False)
    intents, _ = decide(FleetState(), [triage], POLICY, [], T0)
    assert dispatched(intents) == []


def test_a_class_with_no_route_is_not_dispatched():
    """Catches `route[0]` on an empty route, an IndexError out of the pure
    core."""
    intents, _ = decide(FleetState(), [ready(9, "docs", klass="spec")], POLICY, [], T0)
    assert dispatched(intents) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_decide_dispatch.py -v`
Expected: FAIL — no `Dispatch` intents emitted for a READY task

- [ ] **Step 3: Implement**

In `src/fleet/decide.py`, add `Dispatch` and `Drain` to the `fleet.model` import
(this is the only phase that emits either), add these two constants beside
`_REAPING`:

```python
_DRAIN_FRACTION = 0.8

# States that hold an area and a slot. REVIEW is live because someone is working
# on the task: it keeps its area until `fleet verdict` moves it out, which is the
# only legal exit (spec §5 ruling 4). Nothing here dispatches for those states --
# a reviewer and an integrator are M3's to start.
_LIVE = ("claimed", "review", "integrating")
```

then add this function immediately after `_unblock`:

```python
def _dispatch(
    ledger: list[Task], policy: Policy, agents: list[AgentState], spent_usd: float
) -> list[Intent]:
    committed = spent_usd + sum(a.cost_usd for a in agents)
    if policy.fleet_usd > 0 and committed >= policy.fleet_usd * _DRAIN_FRACTION:
        # Drain, do not kill: in-flight tasks finish. Killing at the ceiling
        # throws away work already paid for, and the tokens are spent either way.
        return [Drain(reason="budget")]

    live = [t for t in ledger if t.state in _LIVE]
    if any(t.klass == "integration" for t in live):
        # Global exclusive lock. The integrator touches every area, so per-area
        # exclusion does not cover it (harness amendment 2).
        return []

    held = {t.area for t in live if t.area}
    free = policy.slots - len(live)
    intents: list[Intent] = []
    for task in sorted(ledger, key=lambda t: t.number):
        if free <= 0:
            break
        if task.state != "ready" or not task.auto_ok:
            continue
        route = policy.route_for(task.klass)
        if not route:
            continue
        if task.area is not None and task.area in held:
            continue
        intents.append(
            Dispatch(
                task=task.number,
                role=route[0],
                base=policy.integration_branch,
                # The attempt already on the task, not the next one: a dispatch
                # does not consume an attempt, a release does. One number,
                # computed here, written by expand().
                attempt=max(task.attempts, 1),
            )
        )
        if task.area is not None:
            held.add(task.area)
        free -= 1
    return intents
```

and in `decide`, on the line after `intents += _unblock(ledger)`:

```python
    intents += _dispatch(ledger, policy, agents, prev.spent_usd)
```

Note `_dispatch` reads `prev.spent_usd`, not the newly-computed total: the
threshold is what was already spent when the tick began, plus what the agents
running right now have spent so far.

The three phases now read the *unmodified* ledger, in REAP → UNBLOCK → DISPATCH
order. A task released or escalated this tick is still `claimed` in `ledger`, so
it still holds its area and its slot until the effects land and the next tick
re-reads. That costs one tick of throughput per reap and is the honest reading:
the labels have not been written yet.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_decide_reap.py tests/test_decide_unblock.py tests/test_decide_dispatch.py -v && uv run ruff check src/fleet/decide.py tests/test_decide_dispatch.py && uv run ruff format --check src/fleet/decide.py tests/test_decide_dispatch.py`
Expected: PASS, 39 tests (16 + 5 + 18), and ruff clean

- [ ] **Step 5: Commit**

```bash
git add src/fleet/decide.py tests/test_decide_dispatch.py
git commit -m "$(cat <<'EOF'
feat(decide): the DISPATCH phase

Area exclusion, the auto-ok gate, the global integration lock and the budget
drain.

decide() gates on the fleet:auto-ok label alone and never infers dispatchability
from area. ownership.md maps both configs/** and gates/** to area:configs while
gates/** is separately non-dispatchable, so issue 6 is legitimately area:configs
AND auto-ok -- its criteria forbid touching gates/. Gating on area would refuse
it, which is one of the four hard fails. Path enforcement belongs to the scout
at triage and the integrator at merge.

The drain threshold is the running total plus what the live agents have spent so
far. cost_usd is cumulative per agent, so re-adding every live agent to the
total every tick spends the same dollars over and over and drains a $25 fleet in
seven ticks of a night that spent three.

The base branch comes from policy.integration_branch. A hardcoded "integration"
branches every agent off the wrong base in any repo that names it otherwise, and
no test would notice because the fixture happens to use that name.

Drain stops dispatch only. Reaping and unblocking continue, or the fleet drains
into a state nothing cleans up.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: `expand.py`

**Files:**
- Create: `src/fleet/expand.py`
- Test: `tests/test_expand.py`

**Interfaces:**
- Consumes: intents and effects from Task 3; `labels` (existing); `POLICY` from `tests/test_decide_reap.py`
- Produces: `expand(intent: Intent, policy: Policy) -> list[Effect]` — the two-argument signature spec §3 declares. The attempt number rides on the intent (ruling A1); there is no `attempts=` keyword and no default.

Two corrections to the effects this emits, both about what a real applier can
do. First, `GhClient` has no combined label edit — only `add_labels` and
`remove_labels` (`src/fleet/gh.py:24-25`) — so an executor applies a `SetLabels`
as two calls in some order. Removing all eight state labels *including the one
being added* therefore strands the task with no state label at all under
add-then-remove, which is the exact defect `SetLabels` exists to prevent.
Excluding the added label from `remove` makes both orders land the same state.

Second, `Assign(task, f"fleet-{task}")` names a GitHub user that does not exist,
and `gh issue edit --add-assignee` resolves the login before it edits anything,
so the whole edit fails. Nothing in M2 knows the identity the executor runs
agents as: `fleet.toml` names none, and `GhClient` has no assignee method in
either direction. So `Dispatch` emits no `Assign` at all. The un-assign on
`Release`/`Escalate` stays — clearing ownership names no user, and a released
task that still shows an owner is the drift REAP exists to clear — and M3's
executor, which knows its own identity, is where the assigning half lands.

- [ ] **Step 1: Write the failing test**

Create `tests/test_expand.py`:

```python
from __future__ import annotations

import pytest

from fleet.expand import expand
from fleet.labels import static_vocabulary
from fleet.model import (
    Advance,
    Assign,
    Dispatch,
    Drain,
    Escalate,
    Release,
    SetLabels,
    StartAgent,
    StopAgent,
    Unblock,
)
from test_decide_reap import POLICY


def labels_of(effects):
    return [e for e in effects if isinstance(e, SetLabels)]


def test_a_state_transition_is_exactly_one_labels_effect():
    """commands._post removes all eight state labels then adds one; a failure
    between the two strands the task with no state label at all, invisible to
    every label query. One effect keeps the two halves from drifting apart."""
    effects = expand(Unblock(task=4), POLICY)
    assert len(labels_of(effects)) == 1
    move = labels_of(effects)[0]
    assert "fleet:ready" in move.add
    assert "fleet:blocked" in move.remove


def test_a_transition_never_removes_the_label_it_adds():
    """GhClient has no combined edit -- only add_labels and remove_labels
    (gh.py:24-25) -- so an executor makes two calls in some order. Catches
    `remove=all_state_labels()`: applied add-then-remove it strands the task
    with no state label at all, the exact defect SetLabels exists to prevent."""
    move = labels_of(expand(Unblock(task=4), POLICY))[0]
    assert set(move.add) & set(move.remove) == set()


def test_dispatch_claims_the_task_and_starts_the_agent():
    effects = expand(Dispatch(task=1, role="implementer", base="integration", attempt=1), POLICY)
    assert StartAgent(task=1, role="implementer", base="integration") in effects
    add = labels_of(effects)[0].add
    assert "fleet:claimed" in add
    assert "fleet:attempt-1" in add


def test_dispatch_writes_the_attempt_number_it_was_given():
    """Catches the hardcoded attempt_label(1), the second writer that disagreed
    with the Release that had already counted the attempt."""
    effects = expand(Dispatch(task=1, role="implementer", base="integration", attempt=2), POLICY)
    assert "fleet:attempt-2" in labels_of(effects)[0].add


def test_dispatch_names_no_assignee():
    """`fleet-1` is not a GitHub login: `gh issue edit --add-assignee fleet-1`
    resolves the user first and fails the whole edit. Nothing in M2 knows the
    identity the executor runs agents as, and GhClient has no assignee method,
    so ownership is written in M3 by the executor that does. Catches a
    fabricated owner going back in."""
    effects = expand(Dispatch(task=1, role="implementer", base="integration", attempt=1), POLICY)
    assert [e for e in effects if isinstance(e, Assign)] == []


def test_release_stops_the_agent_unassigns_and_writes_the_attempt():
    effects = expand(Release(task=1, reason="stalled", attempt=2), POLICY)
    assert StopAgent(task=1) in effects
    assert Assign(task=1, owner=None) in effects
    add = labels_of(effects)[0].add
    assert "fleet:ready" in add
    assert "fleet:attempt-2" in add


def test_release_never_emits_an_attempt_label_beyond_the_cap():
    """labels.static_vocabulary is sized from max_attempts, so a label past the
    cap is one gh has never heard of -- and gh refuses an unknown label and
    changes nothing at all. Drift releases are not capped, so this arrives."""
    effects = expand(Release(task=1, reason="no_agent", attempt=3), POLICY)
    add = labels_of(effects)[0].add
    assert "fleet:attempt-3" not in add
    assert "fleet:attempt-2" in add


def test_a_zero_attempt_raises_rather_than_being_rounded_up():
    """Catches a clamp on the low end, which is the silent-reset defect the
    counter used to have: a caller that forgot the number wrote attempt-1 over a
    task already on its second try."""
    with pytest.raises(ValueError, match="attempts start at 1"):
        expand(Release(task=1, reason="stalled", attempt=0), POLICY)


def test_escalate_reaches_human_and_stops_the_agent():
    effects = expand(Escalate(task=1, reason="attempts_exhausted"), POLICY)
    assert StopAgent(task=1) in effects
    assert "fleet:human" in labels_of(effects)[0].add


def test_advance_moves_to_the_named_state_and_stops_the_finished_agent():
    """The agent that wrote the packet has exited. Catches leaving it in the
    executor's live set, where it keeps reporting a cumulative cost every tick
    and drains the fleet budget on a process that finished."""
    effects = expand(Advance(task=1, to="review"), POLICY)
    assert StopAgent(task=1) in effects
    assert "fleet:review" in labels_of(effects)[0].add


def test_advance_refuses_a_state_outside_the_vocabulary():
    """A packet's `to` reaches here from an issue comment, so it is not
    necessarily a state this repo's label set contains."""
    with pytest.raises(ValueError, match="unknown state"):
        expand(Advance(task=1, to="merged"), POLICY)


def test_drain_produces_no_effects():
    """Drain is a decision not to dispatch. There is nothing to write."""
    assert expand(Drain(reason="budget"), POLICY) == []


def test_every_label_written_is_within_the_static_vocabulary():
    """gh resolves a label to an id before adding or removing it, so naming a
    label the repo has never had fails the whole edit. FakeGh mirrors that."""
    vocabulary = static_vocabulary(POLICY.max_attempts, POLICY.max_fix_passes)
    for intent in [
        Unblock(task=4),
        Dispatch(task=1, role="implementer", base="integration", attempt=1),
        Release(task=1, reason="stalled", attempt=2),
        Escalate(task=1, reason="attempts_exhausted"),
        Advance(task=1, to="review"),
    ]:
        for effect in labels_of(expand(intent, POLICY)):
            assert set(effect.add) <= vocabulary
            assert set(effect.remove) <= vocabulary
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_expand.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.expand'`

- [ ] **Step 3: Implement**

Create `src/fleet/expand.py`:

```python
"""Intent -> effects. Pure.

Kept separate from decide() so that decide()'s tests read as policy -- they
assert Dispatch(5, implementer, 1), not a list of label strings -- while the
mutation sequence still gets covered. That sequence needs the coverage:
commands._post removes all eight state labels and then adds one, and a failure
between the two leaves a task with no state label at all.
"""

from __future__ import annotations

from fleet import labels as labels_module
from fleet.model import (
    Advance,
    Assign,
    Dispatch,
    Drain,
    Effect,
    Escalate,
    Intent,
    Policy,
    Release,
    SetLabels,
    StartAgent,
    StopAgent,
    Unblock,
)


def _to_state(task: int, state: str, extra: tuple[str, ...] = ()) -> SetLabels:
    """One effect carrying both halves of the move.

    `GhClient` has no combined edit -- only `add_labels` and `remove_labels`
    (gh.py:24-25) -- so an executor makes two calls in some order. The label
    being added is therefore excluded from `remove`: leaving it in strands the
    task with no state label at all under add-then-remove, which is the exact
    defect this effect exists to prevent.
    """
    add = (labels_module.state_label(state), *extra)
    return SetLabels(
        task=task,
        add=add,
        remove=tuple(label for label in labels_module.all_state_labels() if label not in add),
    )


def _attempt_label(attempt: int, policy: Policy) -> str:
    """Clamped at max_attempts. labels.static_vocabulary is sized from that
    number, so attempt-(cap+1) is a label gh has never heard of -- and gh
    refuses an unknown label by failing the whole edit and changing nothing.

    Clamped at the top only. An attempt of 0 raises out of labels.attempt_label,
    because a zero attempt is the silent-reset defect this counter used to have,
    not a number to round up."""
    return labels_module.attempt_label(min(attempt, policy.max_attempts))


def expand(intent: Intent, policy: Policy) -> list[Effect]:
    if isinstance(intent, Drain):
        return []

    if isinstance(intent, Dispatch):
        # No Assign: `fleet-1` is not a GitHub login, and gh fails the whole
        # edit on an assignee it cannot resolve. Nothing in M2 knows the
        # identity the executor runs agents as -- fleet.toml names none and
        # GhClient has no assignee method -- so ownership is written in M3 by
        # the executor that does.
        return [
            _to_state(intent.task, "claimed", (_attempt_label(intent.attempt, policy),)),
            StartAgent(task=intent.task, role=intent.role, base=intent.base),
        ]

    if isinstance(intent, Release):
        return [
            StopAgent(task=intent.task),
            Assign(task=intent.task, owner=None),
            _to_state(intent.task, "ready", (_attempt_label(intent.attempt, policy),)),
        ]

    if isinstance(intent, Escalate):
        return [
            StopAgent(task=intent.task),
            Assign(task=intent.task, owner=None),
            _to_state(intent.task, "human"),
        ]

    if isinstance(intent, Unblock):
        return [_to_state(intent.task, "ready")]

    if isinstance(intent, Advance):
        if intent.to not in labels_module.STATES:
            # A packet's `to` arrives from an issue comment, so it is not
            # necessarily a state this repo's label set contains.
            raise ValueError(f"unknown state in packet: {intent.to!r}")
        # StopAgent because the agent that wrote the packet has exited. Left in
        # the executor's live set it keeps reporting cost every tick, and the
        # budget drain is computed from live agents.
        return [StopAgent(task=intent.task), _to_state(intent.task, intent.to)]

    raise ValueError(f"cannot expand {intent!r}")
```

Note: `state_label` already raises `ValueError("unknown state: ...")`; the
explicit check in `Advance` exists so the message names the packet as the
source. Every intent in the `Intent` union has a branch here, so the final
`raise` is unreachable by construction — it is the guard for a union member
added later without an expansion.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_expand.py -v && uv run ruff check src/fleet/expand.py tests/test_expand.py && uv run ruff format --check src/fleet/expand.py tests/test_expand.py`
Expected: PASS, 13 tests, and ruff clean

- [ ] **Step 5: Commit**

```bash
git add src/fleet/expand.py tests/test_expand.py
git commit -m "$(cat <<'EOF'
feat(expand): intents to effects, purely

A state transition is one SetLabels effect carrying both the additions and the
removals, and the label being added is excluded from the removals. GhClient has
no combined edit -- only add_labels and remove_labels -- so an executor makes
two calls in some order, and removing the label just added strands the task with
no state label at all under one of them. That is the very failure this effect
was introduced to prevent.

The attempt number rides on the intent. Defaulting it to zero in expand() while
Dispatch hardcoded attempt-1 gave one counter two writers that disagreed; now
decide() computes the number once and expand() writes what it was given, clamped
at max_attempts because static_vocabulary is sized from that number and gh
refuses a label it has never heard of by failing the whole edit.

Dispatch emits no assignee. `fleet-1` is not a GitHub user, and gh resolves an
assignee before editing anything, so the fabricated login would have failed the
whole call against the real API. Nothing in M2 knows the identity the executor
runs agents as; M3's executor does, and the assigning half lands there.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: `executor.py` — the seam and the fake

The fake is the only thing standing between a scenario and reality, so every
place it is more permissive or less observable than a real agent is a place a
scenario can pass while proving nothing. Four such places are closed here.

`clean_exit` carries the packet spec §6 names it with. Without one it produces a
done agent whose task has nothing to advance to — the happy path is unreachable
and looks like a healthy no-op.

`conflict` is dropped. At the `AgentState` seam a conflict is a dirty exit with
the same status and the same everything, so `decide()` cannot tell them apart and
no scenario can assert anything about it that a dirty exit does not already
assert. Conflict-storm coverage waits for an executor that can report a reason;
Task 17 records the deferral. `vanished` takes its place in the vocabulary,
because ledger drift — herdr losing an agent while the ledger still says CLAIMED
— is the one failure the allocator sees as an *absence* rather than a status, and
nothing else in the vocabulary can produce it.

Costs are multiples of `per_task_usd` rather than dollar literals. `3.0` worked
only because the fixture's ceiling happens to be `2.0`; raising the ceiling past
3.0 in `fleet.toml` turned `over_budget` into a healthy agent silently, which is
the exact class of quiet permissiveness the fake exists to prevent.

And a tick's scripting is consumed by that tick. Sticky scripting made spec §7's
own `outcomes: {}  # nothing running yet` mean "keep the previous failure",
which is the opposite of how every reader reads it.

**Files:**
- Create: `src/fleet/executor.py`
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: effects and `AgentState` from Task 3
- Produces: `Executor` Protocol with `apply(effects: list[Effect]) -> None` and `agents() -> list[AgentState]`; `FakeExecutor(per_task_usd: float)` with `script(task, outcome, packet=None)`, `tick()`, `packets() -> dict[int, dict]`, and `applied: list[Effect]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_executor.py`:

```python
from __future__ import annotations

import pytest

from fleet.executor import FakeExecutor
from fleet.model import Assign, SetLabels, StartAgent, StopAgent

from test_decide_reap import POLICY

PACKET = {"to": "review", "head_sha": "a91c204"}


def executor() -> FakeExecutor:
    return FakeExecutor(per_task_usd=POLICY.per_task_usd)


def started(ex: FakeExecutor, task: int = 1) -> None:
    ex.apply([StartAgent(task=task, role="implementer", base="integration")])


def test_starting_an_agent_makes_it_visible_as_working():
    ex = executor()
    started(ex)
    assert [a.task for a in ex.agents()] == [1]
    assert ex.agents()[0].status == "working"


def test_a_working_agents_sequence_advances_each_tick():
    """Stall detection reads a moving sequence, so a healthy agent must move.
    A fake whose sequence never moved would make every agent look stalled."""
    ex = executor()
    started(ex)
    first = ex.agents()[0].change_seq
    ex.tick()
    assert ex.agents()[0].change_seq > first


def test_a_stalled_agent_stops_advancing_but_stays_working():
    """The exact shape of the failure: nominally running, producing nothing."""
    ex = executor()
    started(ex)
    ex.script(1, "stall")
    ex.tick()
    seq = ex.agents()[0].change_seq
    ex.script(1, "stall")
    ex.tick()
    assert ex.agents()[0].change_seq == seq
    assert ex.agents()[0].status == "working"


def test_a_scripted_outcome_lasts_exactly_one_tick():
    """A tick's outcomes are the complete scripting for that tick. Sticky
    scripting made `outcomes: {}` mean "keep the previous failure", so a
    scenario could never show an agent recovering -- and the tick that reads as
    a reset is the one nobody re-checks."""
    ex = executor()
    started(ex)
    ex.script(1, "stall")
    ex.tick()
    seq = ex.agents()[0].change_seq
    ex.tick()
    assert ex.agents()[0].change_seq > seq


def test_a_clean_exit_reports_done_and_hands_over_its_packet():
    """The packet is the whole point of the outcome: REAP advances a finished
    task to `packet["to"]` and emits nothing at all without one."""
    ex = executor()
    started(ex)
    ex.script(1, "clean_exit", packet=PACKET)
    ex.tick()
    assert ex.agents()[0].status == "done"
    assert ex.packets() == {1: PACKET}


def test_a_clean_exit_without_a_packet_is_refused():
    """Spec §6 names the outcome `clean_exit(packet)`. Scripted without one it
    produces a done agent the allocator can do nothing with, which is
    indistinguishable from a healthy tick and passes as one."""
    with pytest.raises(ValueError, match="packet"):
        executor().script(1, "clean_exit")


def test_a_packet_on_an_outcome_that_hands_nothing_over_is_refused():
    """A packet on a stall would be a scenario author believing a task advanced
    when it did not."""
    with pytest.raises(ValueError, match="hands over no packet"):
        executor().script(1, "stall", packet=PACKET)


def test_a_finished_agent_is_gone_by_the_next_tick():
    """A process that exited is not live. Left in place it holds a slot forever,
    so a task that finished could never be replaced and no simulation could get
    past a full fleet."""
    ex = executor()
    started(ex)
    ex.script(1, "clean_exit", packet=PACKET)
    ex.tick()
    assert [a.task for a in ex.agents()] == [1]
    ex.tick()
    assert ex.agents() == []


def test_a_dirty_exit_reports_dirty():
    ex = executor()
    started(ex)
    ex.script(1, "dirty_exit")
    ex.tick()
    assert ex.agents()[0].status == "dirty"


def test_a_vanished_agent_disappears_rather_than_reporting_a_status():
    """Ledger drift is the one failure the allocator reads as an absence: the
    ledger says CLAIMED and herdr has no agent. Modelled as a status it would be
    reaped by reason instead, which is a different rule and a different test."""
    ex = executor()
    started(ex)
    ex.script(1, "vanished")
    ex.tick()
    assert ex.agents() == []


def test_over_budget_accrues_cost_past_the_policys_ceiling():
    """Asserted against `policy.per_task_usd`, not a literal. The magic `3.0`
    only exceeded the ceiling because the fixture's happened to be 2.0."""
    ex = executor()
    started(ex)
    ex.script(1, "over_budget")
    ex.tick()
    assert ex.agents()[0].cost_usd > POLICY.per_task_usd


def test_a_healthy_agent_stays_under_the_ceiling_for_a_whole_scenario():
    """The drip must not trip the per-task budget on its own: an agent alive for
    twenty ticks reaching `over_budget` untouched would release healthy tasks in
    every long scenario and be read as a budget rule working."""
    ex = executor()
    started(ex)
    for _ in range(20):
        ex.tick()
    assert ex.agents()[0].cost_usd < POLICY.per_task_usd


def test_stopping_an_agent_removes_it():
    ex = executor()
    started(ex)
    ex.apply([StopAgent(task=1)])
    assert ex.agents() == []


def test_a_restarted_agent_carries_no_packet_from_its_last_attempt():
    """Otherwise a re-dispatched task advances on the packet of the attempt that
    failed -- the poison task would `Advance` on attempt 2 rather than escalate."""
    ex = executor()
    started(ex)
    ex.script(1, "clean_exit", packet=PACKET)
    ex.tick()
    ex.apply([StopAgent(task=1)])
    started(ex)
    assert ex.packets() == {}


def test_applied_effects_are_recorded_in_order():
    """Scenarios assert on what was written, so the record is the interface."""
    ex = executor()
    effects = [SetLabels(task=1, add=("fleet:ready",)), Assign(task=1, owner=None)]
    ex.apply(effects)
    assert ex.applied == effects


def test_an_unknown_outcome_is_refused():
    """A typo in a scenario file would otherwise silently script a healthy agent
    and the scenario would pass while proving nothing."""
    with pytest.raises(ValueError, match="unknown outcome"):
        executor().script(1, "explodes")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_executor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.executor'`

- [ ] **Step 3: Implement**

Create `src/fleet/executor.py`:

```python
"""The executor seam, with the fake behind it.

Building the seam now is nearly free because decide() is already pure.
Retrofitting it after a daemon exists is not -- and the failure modes that
matter most overnight (stall, poison task, ledger drift, budget drain) are the
hardest to reproduce with real agents. Scripting them is the only way they
become tests.
"""

from __future__ import annotations

from typing import Protocol

from fleet.model import AgentState, Effect, StartAgent, StopAgent

# `conflict` is deliberately absent. At this seam it is a dirty exit -- same
# status, same everything -- so decide() cannot distinguish the two and no
# scenario could assert anything a dirty exit does not already prove. `vanished`
# replaces it: ledger drift is the one failure the allocator reads as an
# absence, and nothing else here can produce one.
OUTCOMES = ("healthy", "clean_exit", "dirty_exit", "stall", "over_budget", "vanished")

_STATUS = {
    "healthy": "working",
    "stall": "working",
    "over_budget": "working",
    "clean_exit": "done",
    "dirty_exit": "dirty",
}

# A process that has exited is not live on the next tick.
_EXITED = frozenset({"done", "dirty"})

# Multiples of the per-task ceiling, never dollars. `3.0` exceeded the ceiling
# only because the fixture's per_task_usd happens to be 2.0; raising it in
# fleet.toml turned `over_budget` into a healthy agent with nothing to notice.
_OVER_BUDGET_MULTIPLE = 3.0
_DRIP_FRACTION = 0.01


class Executor(Protocol):
    """What decide()'s shell needs. `script`/`tick`/`packets` are the fake's
    simulation surface and are deliberately not part of it."""

    def apply(self, effects: list[Effect]) -> None: ...
    def agents(self) -> list[AgentState]: ...


class FakeExecutor:
    """Scripted outcomes, no I/O. The only executor M2 has.

    `per_task_usd` is required rather than defaulted: a default is a second
    place the budget is written down, and the two disagree the moment
    `fleet.toml` changes.
    """

    def __init__(self, per_task_usd: float) -> None:
        self.applied: list[Effect] = []
        self.per_task_usd = per_task_usd
        self._script: dict[int, tuple[str, dict | None]] = {}
        self._live: dict[int, AgentState] = {}
        self._packets: dict[int, dict] = {}
        self._seq = 0

    def script(self, task: int, outcome: str, packet: dict | None = None) -> None:
        if outcome not in OUTCOMES:
            # A typo would otherwise script a healthy agent, and the scenario
            # would pass while proving nothing.
            raise ValueError(f"unknown outcome {outcome!r}; expected one of {OUTCOMES}")
        if outcome == "clean_exit" and not packet:
            raise ValueError(
                "clean_exit needs a packet: without one REAP has nothing to advance to, "
                "and a finished task looks identical to a healthy one"
            )
        if outcome != "clean_exit" and packet is not None:
            raise ValueError(f"{outcome!r} hands over no packet")
        self._script[task] = (outcome, packet)

    def apply(self, effects: list[Effect]) -> None:
        self.applied.extend(effects)
        for effect in effects:
            if isinstance(effect, StartAgent):
                self._seq += 1
                # A new attempt has produced nothing yet. Keeping the last
                # attempt's packet would advance a task on the work that failed.
                self._packets.pop(effect.task, None)
                self._live[effect.task] = AgentState(
                    task=effect.task, status="working", change_seq=self._seq, cost_usd=0.0
                )
            elif isinstance(effect, StopAgent):
                self._live.pop(effect.task, None)
                self._packets.pop(effect.task, None)

    def tick(self) -> None:
        """Advance every live agent by the outcome scripted for THIS tick."""
        for task, agent in list(self._live.items()):
            if agent.status in _EXITED:
                # It exited on the previous tick. Left live it would hold a slot
                # forever, so a task that finished could never be replaced.
                del self._live[task]
                continue
            outcome, packet = self._script.get(task, ("healthy", None))
            if outcome == "vanished":
                del self._live[task]
                continue
            self._seq += 1
            if packet is not None:
                self._packets[task] = packet
            self._live[task] = AgentState(
                task=task,
                status=_STATUS[outcome],
                # A stalled agent is nominally running and producing nothing, so
                # its sequence must NOT move -- that is the only signal there is.
                change_seq=agent.change_seq if outcome == "stall" else self._seq,
                cost_usd=agent.cost_usd + self._cost(outcome),
            )
        # Consumed by the tick it was written for.
        self._script.clear()

    def _cost(self, outcome: str) -> float:
        if outcome == "over_budget":
            return self.per_task_usd * _OVER_BUDGET_MULTIPLE
        return self.per_task_usd * _DRIP_FRACTION

    def agents(self) -> list[AgentState]:
        return sorted(self._live.values(), key=lambda a: a.task)

    def packets(self) -> dict[int, dict]:
        """What finished agents handed over. The shell reads these off the issue
        after a clean exit; here they come straight from the script."""
        return dict(self._packets)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_executor.py -v && uv run ruff check src/fleet/executor.py tests/test_executor.py`
Expected: PASS, 16 tests, ruff clean

- [ ] **Step 5: Commit**

```bash
git add src/fleet/executor.py tests/test_executor.py
git commit -m "$(cat <<'EOF'
feat(executor): the seam and a scripted fake

A stalled agent keeps its change_seq and stays "working". That is the whole
shape of the failure -- nominally running, producing nothing -- and the sequence
not moving is the only signal there is, because herdr's snapshot carries no
timestamp.

clean_exit carries a packet, because REAP advances a finished task to
packet["to"] and emits nothing without one: scripted packetless, the happy path
is unreachable and reads as a healthy tick.

`conflict` is dropped and `vanished` replaces it. At this seam a conflict is a
dirty exit with the same status, so nothing could assert it; a vanished agent is
ledger drift, which the allocator reads as an absence and no other outcome can
produce.

Costs are multiples of per_task_usd rather than dollar literals, and a tick's
scripting is consumed by that tick. Both were silent permissiveness: a ceiling
raised in fleet.toml turned over_budget healthy, and sticky scripting made
`outcomes: {}` mean "keep the previous failure".

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: `runlog.py`

Spec §5 says every intent and its outcome appends to a timestamped JSONL run
log. A module with tests and no caller does not do that: it ships dark, and the
claim in the spec stays a claim. `simulate.run` takes the log as an optional
argument (Task 14) and appends every intent of every tick, which is the caller
this file was always described as having. `fleet plan --dry-run` deliberately
does not write one — it writes nothing, by definition.

**Files:**
- Create: `src/fleet/runlog.py`
- Test: `tests/test_runlog.py`

**Interfaces:**
- Consumes: intents from Task 3
- Produces: `RunLog(path: Path, run_id: str)` with keyword-only `append(*, tick: int, at: datetime, intent: Intent, outcome: str) -> None`, and a module-level `read(path) -> list[dict]`.
- Consumed by: `simulate.run(scenario, root, runlog=...)` (Task 14)

- [ ] **Step 1: Write the failing test**

Create `tests/test_runlog.py`:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone

from fleet.model import Dispatch, Release, Unblock
from fleet.runlog import RunLog, read

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)


def dispatch(task=1, attempt=1):
    return Dispatch(task=task, role="implementer", base="integration", attempt=attempt)


def test_each_entry_is_one_json_line(tmp_path):
    """One line per entry, not one array per run: a night that dies at 4am must
    leave a parseable prefix rather than a truncated `[`."""
    log = RunLog(tmp_path / "run.jsonl", run_id="r1")
    log.append(tick=1, at=T0, intent=dispatch(), outcome="applied")
    log.append(tick=2, at=T0, intent=Release(task=1, reason="stalled", attempt=2),
               outcome="applied")
    lines = (tmp_path / "run.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["intent"]["kind"] == "Dispatch"


def test_every_field_of_the_intent_is_recorded_not_just_its_name(tmp_path):
    """The attempt number is the field the poison-task and stall claims turn on.
    A log that recorded only the kind could not answer "which attempt was that"
    after the labels moved on."""
    log = RunLog(tmp_path / "run.jsonl", run_id="r1")
    log.append(tick=1, at=T0, intent=Release(task=4, reason="stalled", attempt=2),
               outcome="applied")
    entry = read(tmp_path / "run.jsonl")[0]
    assert entry["intent"] == {"kind": "Release", "task": 4, "reason": "stalled", "attempt": 2}
    assert entry["at"] == T0.isoformat()


def test_a_transient_label_survives_in_the_log(tmp_path):
    """m2-inputs §3: rubric 04 asserts fleet:blocked was applied, but the next
    transition removes it, so the final ledger cannot answer "was it ever
    applied". Only this file can -- and only if it appends per event rather than
    keeping the latest entry per task, which is the same erasure one level up."""
    log = RunLog(tmp_path / "run.jsonl", run_id="r1")
    log.append(tick=1, at=T0, intent=Unblock(task=4), outcome="applied")
    log.append(tick=2, at=T0, intent=dispatch(task=4), outcome="applied")
    kinds = [e["intent"]["kind"] for e in read(tmp_path / "run.jsonl") if e["intent"]["task"] == 4]
    assert kinds == ["Unblock", "Dispatch"]


def test_appending_never_truncates_an_existing_log(tmp_path):
    """A run that dies mid-night must leave everything up to that point intact."""
    path = tmp_path / "run.jsonl"
    RunLog(path, run_id="r1").append(tick=1, at=T0, intent=Unblock(task=4), outcome="applied")
    RunLog(path, run_id="r1").append(tick=2, at=T0, intent=Unblock(task=5), outcome="applied")
    assert len(read(path)) == 2


def test_the_run_id_is_on_every_entry(tmp_path):
    log = RunLog(tmp_path / "run.jsonl", run_id="night-3")
    log.append(tick=1, at=T0, intent=Unblock(task=4), outcome="applied")
    assert read(tmp_path / "run.jsonl")[0]["run_id"] == "night-3"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_runlog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.runlog'`

- [ ] **Step 3: Implement**

Create `src/fleet/runlog.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_runlog.py -v && uv run ruff check src/fleet/runlog.py tests/test_runlog.py`
Expected: PASS, 5 tests, ruff clean

- [ ] **Step 5: Commit**

```bash
git add src/fleet/runlog.py tests/test_runlog.py
git commit -m "$(cat <<'EOF'
feat(runlog): append-only JSONL of every intent

Harness §7 makes this one of the three inputs `fleet grade` needs. simulate.run
appends every intent of every tick it replays, so the log has a caller in M2
rather than shipping dark against a spec line that claims it is written.

It is also the only place a transient label survives: rubric 04 asserts
fleet:blocked was applied, and the next transition removes it, so the final
ledger cannot answer the question. That only holds because entries append per
event -- a log keyed by task would erase the same history one level up.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 14: `simulate.py` — the scenario runner

The scenario schema is the contract Task 15's seven files are written against, so
every hole in it is a hole in the proof. Three are closed here. `expect` entries
are parsed structurally rather than string-compared, because pyyaml reads the
spec's own `expect: [Dispatch(5, implementer, 1)]` as the three strings
`Dispatch(5`, `implementer`, `1)` — an unquoted entry can never match a rendered
intent, so a scenario written in the spec's style asserts nothing while looking
like it asserts everything. Per-tick keys are validated, because `expects:` for
`expect:` is the same failure one level down and it is silent. And `expect` is
mandatory on every tick: an omitted key meant "do not check", which is exactly
the vacuity the task's own commit message claims to have designed out.

Two facts have to reach `decide()` or two of its three phases are unreachable.
`Task.packet` is where `blocked_on` lives and where a clean exit's target state
lives, so with no packets `clean_exit` can never produce `Advance` and UNBLOCK
can never fire. `seed.packets` seeds the ledger's standing packets; a tick's
`packets:` carries the packet an agent hands over as it finishes.

Each intent is expanded exactly once and the result is reused for the executor,
the row fold and the run log. Expanding twice with different attempt numbers made
`FakeExecutor.applied` — documented as the surface scenarios assert on — record a
label sequence that never happened.

**Files:**
- Create: `src/fleet/simulate.py`
- Test: `tests/test_simulate.py`

**Interfaces:**
- Consumes: `decide`, `expand`, `FakeExecutor`, `ledger.read`, `policy.load`, `RunLog`
- Produces: `run(scenario: dict, root: Path, runlog: RunLog | None = None) -> list[list[Intent]]` (one entry per tick), `load_scenario(path: Path) -> dict`, `render_intent(intent: Intent) -> str`, and `ScenarioError`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_simulate.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import FIXTURE_POLICY
from fleet.model import Advance, Dispatch, Release
from fleet.runlog import RunLog
from fleet.runlog import read as read_runlog
from fleet.simulate import ScenarioError, load_scenario, render_intent, run

ROOT = Path(__file__).resolve().parents[1]

CAPTURED = "fixtures/ledger/issues.json"

INLINE = {
    "name": "one-dispatch",
    "proves": "smoke",
    "seed": {
        "issues": [
            {
                "number": 1,
                "title": "t",
                "state": "OPEN",
                "labels": ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"],
                "assignees": [],
            }
        ],
        "policy": str(FIXTURE_POLICY),
    },
    "ticks": [{"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]}],
}


def test_an_inline_scenario_dispatches():
    ticks = run(INLINE, ROOT)
    assert Dispatch(task=1, role="implementer", base="integration", attempt=1) in ticks[0]


def test_expectations_are_checked_not_merely_recorded():
    """A harness that records without asserting is a harness that always passes."""
    wrong = {**INLINE, "ticks": [{"outcomes": {}, "expect": ["Dispatch(99, implementer, 1)"]}]}
    with pytest.raises(AssertionError, match="tick 1"):
        run(wrong, ROOT)


def test_an_expectation_that_omits_the_attempt_number_does_not_match():
    """The attempt number is half of every stall and poison-task claim. If the
    renderer dropped it, `Release(1, stalled)` would match a release on any
    attempt at all, including the third one that must never happen."""
    stale = {**INLINE, "ticks": [{"outcomes": {}, "expect": ["Dispatch(1, implementer)"]}]}
    with pytest.raises(AssertionError, match="tick 1"):
        run(stale, ROOT)


def test_an_unquoted_expect_entry_is_refused_rather_than_silently_unmatched():
    """pyyaml reads flow-style `[Dispatch(1, implementer, 1)]` as three separate
    strings, none of which can ever equal a rendered intent. Compared as strings
    that is a scenario which fails for the wrong reason -- or, on a tick whose
    other entries happen to match, one that passes having asserted garbage."""
    text = (
        "name: n\n"
        "proves: p\n"
        "seed:\n"
        "  issues: []\n"
        "  policy: " + str(FIXTURE_POLICY) + "\n"
        "ticks:\n"
        "  - outcomes: {}\n"
        "    expect: [Dispatch(1, implementer, 1)]\n"
    )
    with pytest.raises(ScenarioError, match="cannot parse"):
        run(yaml.safe_load(text), ROOT)


def test_an_expectation_naming_something_that_is_not_an_intent_is_refused():
    typo = {**INLINE, "ticks": [{"outcomes": {}, "expect": ["Dispath(1, implementer, 1)"]}]}
    with pytest.raises(ScenarioError, match="not an intent"):
        run(typo, ROOT)


def test_an_empty_expect_asserts_that_nothing_happened():
    """The poison-task scenario's final tick asserts an absence. If `expect: []`
    meant "do not check", the claim it proves would be unproven."""
    quiet = {
        **INLINE,
        "seed": {"issues": [], "policy": str(FIXTURE_POLICY)},
        "ticks": [{"outcomes": {}, "expect": []}],
    }
    run(quiet, ROOT)

    noisy = {**INLINE, "ticks": [{"outcomes": {}, "expect": []}]}
    with pytest.raises(AssertionError, match="tick 1"):
        run(noisy, ROOT)


def test_a_captured_ledger_seed_dispatches_every_free_area_at_once():
    """The captured ledger is the default seed, and this is the shape it has:
    eleven ready tasks, one live REVIEW task holding area:docs and one of the six
    slots, so five areas are free and five tasks dispatch. A comparison that
    listed one Dispatch would be a comparison that is not exhaustive."""
    scenario = {
        "name": "captured",
        "proves": "real shapes",
        "seed": {"ledger": CAPTURED, "policy": str(FIXTURE_POLICY)},
        "ticks": [
            {
                "outcomes": {},
                "expect": [
                    "Dispatch(1, implementer, 1)",
                    "Dispatch(2, implementer, 1)",
                    "Dispatch(4, implementer, 1)",
                    "Dispatch(5, implementer, 1)",
                    "Dispatch(6, implementer, 1)",
                ],
            }
        ],
    }
    assert len(run(scenario, ROOT)[0]) == 5


def test_a_repo_relative_policy_path_is_joined_to_root(monkeypatch, tmp_path):
    """Every scenario file writes `policy: fixtures/policy/fleet-fixture`.
    Unjoined, that is resolved against the process CWD and `_find_root` walks
    UP from there -- so running pytest from anywhere else silently loads some
    other repo's fleet.toml, or none, and the scenario proves a claim about a
    policy it never read."""
    monkeypatch.chdir(tmp_path)
    scenario = {
        **INLINE,
        "seed": {**INLINE["seed"], "policy": "fixtures/policy/fleet-fixture"},
    }
    run(scenario, ROOT)


def test_time_advances_by_one_stall_threshold_per_tick():
    """A stall takes stall_minutes of simulated time and no wall-clock at all,
    and it takes two frozen ticks: a first-seen agent has idle == 0."""
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            {"outcomes": {1: "stall"}, "expect": []},
            {"outcomes": {1: "stall"}, "expect": ["Release(1, stalled, 2)"]},
        ],
    }
    ticks = run(scenario, ROOT)
    assert Release(task=1, reason="stalled", attempt=2) in ticks[2]


def test_a_scripted_outcome_lasts_one_tick_only():
    """A tick's `outcomes:` is the complete scripting for that tick. Sticky
    scripting made spec §7's `outcomes: {}  # nothing running yet` mean "keep
    yesterday's failure", so a scenario could never show an agent recovering --
    and the tick that looks like a reset is the one every reader trusts."""
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            {"outcomes": {1: "stall"}, "expect": []},
            {"outcomes": {}, "expect": []},
            {"outcomes": {}, "expect": []},
        ],
    }
    run(scenario, ROOT)


def test_a_clean_exit_advances_the_task_to_its_packets_target():
    """The happy path. Without a packet on the outcome, `clean_exit` produces a
    done agent whose task has nothing to advance to, and no scenario in the set
    can move a task past `claimed`."""
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            {
                "outcomes": {1: "clean_exit"},
                "packets": {1: {"to": "review", "head_sha": "a91c204"}},
                "expect": ["Advance(1, review)"],
            },
        ],
    }
    assert Advance(task=1, to="review") in run(scenario, ROOT)[1]


def test_a_seeded_packet_supplies_the_dependency_an_unblock_needs():
    """blocked_on is read off the packet, never a label -- there is no
    fleet:blocked-on-N. With no packets seeded, `blocked_on` is None for every
    task and the whole UNBLOCK phase is unreachable."""
    scenario = {
        "name": "blocked",
        "proves": "unblock needs a packet",
        "seed": {
            "policy": str(FIXTURE_POLICY),
            "packets": {4: {"to": "blocked", "blocked_on": 11}},
            "issues": [
                {
                    "number": 4,
                    "title": "t",
                    "state": "OPEN",
                    "labels": ["fleet:blocked", "fleet:auto-ok", "area:models", "class:patch"],
                    "assignees": [],
                },
                {
                    "number": 11,
                    "title": "t",
                    "state": "OPEN",
                    "labels": ["fleet:done", "fleet:auto-ok", "area:data", "class:patch"],
                    "assignees": [],
                },
            ],
        },
        "ticks": [{"outcomes": {}, "expect": ["Unblock(4)"]}],
    }
    run(scenario, ROOT)


def test_the_run_log_records_every_intent_of_every_tick(tmp_path):
    """Spec §5 requires the log and the fold already returns exactly what it
    records. Wired to nothing, it ships dark and rubric 04 still cannot answer
    "was fleet:blocked ever applied"."""
    log = RunLog(tmp_path / "run.jsonl", run_id="sim")
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            {"outcomes": {1: "dirty_exit"}, "expect": ["Release(1, dirty_exit, 2)"]},
        ],
    }
    run(scenario, ROOT, runlog=log)
    entries = read_runlog(tmp_path / "run.jsonl")
    assert [(e["tick"], e["intent"]["kind"]) for e in entries] == [(1, "Dispatch"), (2, "Release")]


def test_the_rows_carry_the_last_ticks_labels_into_the_next():
    """The attempt counter lives in a label, so a fold that does not write the
    label back re-dispatches attempt 1 forever and the cap never binds."""
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            {"outcomes": {1: "dirty_exit"}, "expect": ["Release(1, dirty_exit, 2)"]},
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 2)"]},
        ],
    }
    run(scenario, ROOT)


def test_an_unknown_scenario_key_is_refused():
    with pytest.raises(ScenarioError, match="unknown key"):
        run({**INLINE, "tickz": []}, ROOT)


def test_an_unknown_tick_key_is_refused():
    """`expects:` for `expect:` read as "do not check", so the file passed while
    proving nothing -- the exact vacuity `expect: []` was designed against, one
    level down."""
    with pytest.raises(ScenarioError, match="tick 1"):
        run({**INLINE, "ticks": [{"outcomes": {}, "expects": []}]}, ROOT)


def test_a_tick_with_no_expectation_is_refused():
    with pytest.raises(ScenarioError, match="no expect"):
        run({**INLINE, "ticks": [{"outcomes": {}}]}, ROOT)


def test_a_scenario_without_ticks_is_refused():
    with pytest.raises(ScenarioError, match="no ticks"):
        run({"name": "x", "proves": "y", "seed": {"issues": [], "policy": "."}}, ROOT)


def test_a_missing_seed_is_a_scenario_error_not_a_key_error():
    """`ScenarioError` says "this file could not be understood". A bare KeyError
    says a traceback, and the one on `name` fires inside the failure message of
    the assertion it was about to report -- destroying the report with a bug in
    the reporter."""
    with pytest.raises(ScenarioError, match="no seed"):
        run({"name": "x", "proves": "y", "ticks": [{"outcomes": {}, "expect": []}]}, ROOT)


def test_a_seed_naming_both_a_ledger_and_inline_issues_is_refused():
    """One silently won. A scenario whose hand-written rows are ignored proves
    something about a ledger its author never looked at."""
    both = {**INLINE, "seed": {**INLINE["seed"], "ledger": CAPTURED}}
    with pytest.raises(ScenarioError, match="both"):
        run(both, ROOT)


def test_render_intent_drops_base_and_nothing_else():
    intent = Dispatch(task=5, role="implementer", base="integration", attempt=2)
    assert render_intent(intent) == "Dispatch(5, implementer, 2)"


def test_load_scenario_reads_yaml(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(INLINE))
    assert load_scenario(path)["name"] == "one-dispatch"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_simulate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.simulate'`

- [ ] **Step 3: Implement**

Create `src/fleet/simulate.py`:

```python
"""Replay a whole night through decide(), offline and deterministically.

No clock and no I/O beyond reading the scenario and its seeds, so a stall that
takes eleven simulated minutes costs no wall-clock at all.

The schema is strict in both directions. An unknown key raises, and so does a
missing one: a scenario is a proof, and the failure mode of a lenient scenario
parser is a file that reports a pass having asserted nothing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from fleet import ledger as ledger_module
from fleet import policy as policy_module
from fleet.config import load_config
from fleet.decide import decide
from fleet.executor import FakeExecutor
from fleet.expand import expand
from fleet.model import (
    Advance,
    Dispatch,
    Drain,
    Effect,
    Escalate,
    FleetState,
    Intent,
    Release,
    SetLabels,
    Unblock,
)
from fleet.runlog import RunLog

START = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)

_KEYS = {"name", "proves", "seed", "ticks"}
_SEED_KEYS = {"issues", "ledger", "packets", "policy"}
_TICK_KEYS = {"expect", "outcomes", "packets"}

# Parsed structurally, not string-compared: pyyaml splits an unquoted
# `[Dispatch(1, implementer, 1)]` into three scalars, and a string comparison
# turns that into a silent mismatch instead of a complaint about the file.
_CALL = re.compile(r"^(\w+)\((.*)\)$")
_INTENTS = (Advance, Dispatch, Drain, Escalate, Release, Unblock)
_INTENT_NAMES = frozenset(cls.__name__ for cls in _INTENTS)


class ScenarioError(Exception):
    """A scenario file could not be understood."""


def load_scenario(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def render_intent(intent: Intent) -> str:
    """`Dispatch(5, implementer, 2)` — the form scenarios are written in.

    Every field in declaration order except `base`, which is `policy
    .integration_branch` for every dispatch M2 makes and so carries no
    information a scenario could assert on. The attempt number is rendered
    precisely because it does: it is the "attempt += 1" half of the stall and
    poison-task claims, and a renderer that dropped it would let
    `Release(1, stalled)` match a release on any attempt, the third included.
    """
    values = [str(v) for k, v in vars(intent).items() if k != "base"]
    return f"{type(intent).__name__}({', '.join(values)})"


def _shape(text: object) -> tuple[str, tuple[str, ...]]:
    """`"Dispatch(5, implementer, 2)"` -> `("Dispatch", ("5", "implementer", "2"))`."""
    if not isinstance(text, str):
        raise ScenarioError(f"cannot parse expectation {text!r}: expected a quoted string")
    match = _CALL.match(text.strip())
    if match is None:
        raise ScenarioError(
            f"cannot parse expectation {text!r}: expected Name(arg, ...), quoted. "
            "An unquoted YAML flow entry splits on every comma."
        )
    name, inside = match.group(1), match.group(2).strip()
    if name not in _INTENT_NAMES:
        raise ScenarioError(f"{name!r} is not an intent; expected one of {sorted(_INTENT_NAMES)}")
    args = tuple(part.strip() for part in inside.split(",")) if inside else ()
    return name, args


def _check(scenario: dict) -> None:
    unknown = set(scenario) - _KEYS
    if unknown:
        raise ScenarioError(f"unknown key(s) in scenario: {sorted(unknown)}")
    for key in ("name", "proves"):
        if not str(scenario.get(key) or "").strip():
            raise ScenarioError(f"scenario has no {key}")
    if not scenario.get("seed"):
        raise ScenarioError("scenario has no seed")
    if not scenario.get("ticks"):
        raise ScenarioError("scenario has no ticks")

    seed = scenario["seed"]
    unknown_seed = set(seed) - _SEED_KEYS
    if unknown_seed:
        raise ScenarioError(f"unknown key(s) in seed: {sorted(unknown_seed)}")
    if not seed.get("policy"):
        raise ScenarioError("seed has no policy")
    if "ledger" in seed and "issues" in seed:
        # One of the two would win silently, so half the file would be a
        # description of a ledger the run never read.
        raise ScenarioError("seed names both a ledger and inline issues; pick one")

    for index, tick in enumerate(scenario["ticks"], start=1):
        unknown_tick = set(tick) - _TICK_KEYS
        if unknown_tick:
            raise ScenarioError(f"unknown key(s) in tick {index}: {sorted(unknown_tick)}")
        if tick.get("expect") is None:
            # Absent meant "do not check", so one mistyped key produced a green
            # scenario proving nothing. `expect: []` is the way to assert an
            # absence; there is no way to assert nothing at all.
            raise ScenarioError(f"tick {index} has no expect; use `expect: []` to assert an absence")


def _label_names(raw: list) -> list[str]:
    """gh returns label objects; a hand-written seed writes names. Normalize
    once, here, so the row fold below never has to know which shape it holds --
    and so simulate does not reach into ledger's private helpers to find out."""
    names = []
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else item
        if not name:
            raise ScenarioError(f"seeded row carries a label with no name: {item!r}")
        names.append(name)
    return names


def _rows(seed: dict, root: Path) -> list[dict]:
    if "ledger" in seed:
        raw = json.loads((root / seed["ledger"]).read_text(encoding="utf-8"))
    else:
        raw = list(seed.get("issues", []))
    return [dict(row, labels=_label_names(row.get("labels", []))) for row in raw]


def _numbered(mapping: dict | None) -> dict[int, dict]:
    """YAML gives `{5: ...}` an int key and `{"5": ...}` a str one."""
    return {int(key): value for key, value in (mapping or {}).items()}


def _apply_to_rows(rows: list[dict], effects: list[Effect]) -> list[dict]:
    """Fold this intent's label effects back into the seeded rows, so the next
    tick reads a ledger that reflects the last one.

    Only `SetLabels` is folded. `decide()` reads no other field a scenario can
    observe -- notably not `assignee` -- and a fold no assertion can see is code
    no test can keep honest.
    """
    updated = [dict(row, labels=list(row["labels"])) for row in rows]
    by_number = {row["number"]: row for row in updated}
    for effect in effects:
        if not isinstance(effect, SetLabels):
            continue
        target = by_number.get(effect.task)
        if target is None:
            continue
        kept = [name for name in target["labels"] if name not in effect.remove]
        target["labels"] = kept + [name for name in effect.add if name not in kept]
    return updated


def run(scenario: dict, root: Path, runlog: RunLog | None = None) -> list[list[Intent]]:
    _check(scenario)
    seed = scenario["seed"]
    # Joined to root exactly as `ledger` is. Unjoined, load_config resolves
    # against the CWD and _find_root walks UP from there, so a relative miss is
    # not an error -- it is some other repo's fleet.toml, silently.
    cfg = load_config(root / seed["policy"])
    policy = policy_module.load(cfg)
    rows = _rows(seed, root)
    seed_packets = _numbered(seed.get("packets"))

    executor = FakeExecutor(per_task_usd=policy.per_task_usd)
    state = FleetState()
    now = START
    history: list[list[Intent]] = []

    for index, tick in enumerate(scenario["ticks"], start=1):
        handovers = _numbered(tick.get("packets"))
        for task, outcome in _numbered(tick.get("outcomes")).items():
            try:
                executor.script(task, outcome, packet=handovers.get(task))
            except ValueError as exc:
                raise ScenarioError(f"tick {index}: {exc}") from exc
        executor.tick()

        packets = {**seed_packets, **executor.packets()}
        tasks = ledger_module.read(rows, packets=packets)
        intents, state = decide(state, tasks, policy, executor.agents(), now)
        history.append(intents)

        for intent in intents:
            # Expanded once and reused. Two expansions with different attempt
            # numbers made FakeExecutor.applied record a label sequence that
            # never happened -- for exactly the counter the claims are about.
            effects = expand(intent, policy)
            executor.apply(effects)
            rows = _apply_to_rows(rows, effects)
            if runlog is not None:
                runlog.append(tick=index, at=now, intent=intent, outcome="applied")

        actual = sorted(_shape(render_intent(i)) for i in intents)
        expected = sorted(_shape(entry) for entry in tick["expect"])
        if actual != expected:
            raise AssertionError(
                f"tick {index} of {scenario['name']}: expected {sorted(tick['expect'])}, "
                f"got {sorted(render_intent(i) for i in intents)}"
            )
        now += timedelta(minutes=policy.stall_minutes)

    return history
```

Note the failure message renders both sides in the scenario's own notation, so
a mismatch can be pasted straight back into the file.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_simulate.py -v && uv run ruff check src/fleet/simulate.py tests/test_simulate.py`
Expected: PASS, 22 tests, ruff clean

- [ ] **Step 5: Commit**

```bash
git add src/fleet/simulate.py tests/test_simulate.py
git commit -m "$(cat <<'EOF'
feat(simulate): replay a night through decide(), offline

`expect` entries are parsed structurally and must be quoted. pyyaml reads the
spec's own `expect: [Dispatch(5, implementer, 1)]` as three separate strings, so
compared as strings no unquoted entry could ever match a rendered intent -- a
scenario that looks like a proof and is not one.

`expect` is mandatory on every tick and unknown tick keys raise. An absent key
meant "do not check", so `expects:` for `expect:` produced a green scenario
asserting nothing: the same vacuity `expect: []` was designed against, one level
down.

Packets reach the simulator, through `seed.packets` for standing dependencies
and a tick's `packets:` for what a finishing agent hands over. Without them
`blocked_on` is None for every task and a clean exit has nothing to advance to,
which left two of decide()'s three phases unreachable by any scenario.

Each intent is expanded once and the result reused for the executor, the row
fold and the run log. Two expansions with different attempt numbers made
FakeExecutor.applied record a label sequence that never happened.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: The scenario set

One file per row of parent §10's failure table and per case in §11 — and, where
M2 cannot honestly reach a row, no file at all, with the gap recorded in Task 17
rather than papered over by a scenario that passes vacuously.

Five of the seven seed from `fixtures/ledger/issues.json`. Spec §7 makes that
deliberate: the pure core is exercised against real captured ledger shapes, not
invented ones, and hand-written rows drifted from the capture they claimed to
stand in for — three files gave issue 1 three different label sets and one
invented an issue 20 the ledger has never had. The two that stay hand-written say
why in their own `proves`, because neither case can be reached from a capture in
which nothing is blocked and no integration has ever run.

The captured ledger's first tick is the shape every one of those five starts
from: eleven READY tasks, plus issue 12 in `fleet:review`, which is a live state
(spec §5 ruling 4) and therefore holds `area:docs` and one of the six slots. Five
areas are free, so five tasks dispatch — 1 (eval), 2 (train), 4 (models), 5
(data), 6 (configs) — and 3, 7, 8, 10 and 11 are excluded as the second task in
an already-held area while 9 is excluded by the reviewing task's area. The
comparison is exhaustive, so every tick lists all five. A file that listed one
would fail, which is why the spec's own example does.

Two consequences are worth stating because they are what makes these files
falsifiable. Every expectation carries its attempt number, so `Release(1,
stalled, 2)` asserts the "attempt += 1" half of parent §10's stall row rather
than leaving it to be believed, and the re-dispatch a tick later asserts it a
second time from the other side: `Dispatch(1, implementer, 2)` is `decide()`
reading back the `fleet:attempt-2` label the release wrote. And an absence is
assertable without writing `expect: []` — poison-task's final tick lists the one
dispatch that does happen, and the claim it proves is what that list does not
contain.

**Files:**
- Create: `fixtures/scenarios/area-exclusion.yaml`
- Create: `fixtures/scenarios/poison-task.yaml`
- Create: `fixtures/scenarios/stall-releases.yaml`
- Create: `fixtures/scenarios/blocked-self-resolves.yaml`
- Create: `fixtures/scenarios/budget-drains.yaml`
- Create: `fixtures/scenarios/ledger-drift.yaml`
- Create: `fixtures/scenarios/integration-lock.yaml`
- Test: `tests/test_scenarios.py`

**Interfaces:**
- Consumes: `run`, `load_scenario` from Task 14
- Produces: nothing importable; the scenarios are data.

- [ ] **Step 1: Write the failing test**

Create `tests/test_scenarios.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from fleet.simulate import load_scenario, run

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = sorted((ROOT / "fixtures/scenarios").glob("*.yaml"))
EXPECTED_STEMS = {
    "area-exclusion",
    "blocked-self-resolves",
    "budget-drains",
    "integration-lock",
    "ledger-drift",
    "poison-task",
    "stall-releases",
}


def test_the_scenario_set_is_exactly_the_files_this_task_names():
    """A glob that matches nothing makes the parametrized tests below vacuous --
    they report a pass having run zero scenarios. `>= 7` is not enough either:
    it passes with one file deleted and two added, which is how a failure-table
    row stops being covered without anything turning red."""
    assert {p.stem for p in SCENARIOS} == EXPECTED_STEMS


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenario(path):
    scenario = load_scenario(path)
    history = run(scenario, ROOT)
    # run() raises on the first tick whose expectation does not hold, so getting
    # here means every tick held. This asserts the other half -- that every tick
    # ran. Without it the test cannot tell "all expectations held" from "the
    # harness stopped after the first one".
    assert len(history) == len(scenario["ticks"])


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_every_scenario_asserts_at_least_one_thing_that_happened(path):
    """`expect: []` is a real assertion of absence, and an allocator that does
    nothing at all satisfies every one of them. A file made only of absences
    proves nothing about an allocator that works."""
    scenario = load_scenario(path)
    assert any(tick["expect"] for tick in scenario["ticks"])


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_every_scenario_cites_the_claim_it_proves(path):
    """Spec §7 makes `proves` the link between a file and the row it covers. A
    sentence citing nothing lets a scenario drift off the failure table with
    nothing to notice, which is the drift the YAML format exists to stop."""
    proves = load_scenario(path)["proves"]
    assert any(source in proves for source in ("parent §10", "parent §11", "harness"))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_scenarios.py -v`
Expected: FAIL — `assert set() == {'area-exclusion', ...}`

- [ ] **Step 3: Write the scenarios**

`fixtures/scenarios/area-exclusion.yaml`:
```yaml
name: area-exclusion
proves: >-
  parent §11 "two READY tasks in one area → exactly one dispatched"; harness §4
  issues 2 and 3. Tick 1 is the positive and negative control together: eleven
  READY tasks, five dispatched, one per free area. Tick 3 shows an area
  re-opening to exactly one of its three waiting tasks once it is vacated.
seed:
  ledger: fixtures/ledger/issues.json
  policy: fixtures/policy/fleet-fixture
ticks:
  # 3, 7, 8, 10 and 11 are each the second READY task in an area already taken;
  # 9 is excluded because issue 12 is in fleet:review, a live state, and holds
  # area:docs.
  - outcomes: {}
    expect:
      - "Dispatch(1, implementer, 1)"
      - "Dispatch(2, implementer, 1)"
      - "Dispatch(4, implementer, 1)"
      - "Dispatch(5, implementer, 1)"
      - "Dispatch(6, implementer, 1)"
  # The fleet is full: five claimed plus one reviewing against slots = 6.
  - outcomes: {1: dirty_exit}
    expect:
      - "Release(1, dirty_exit, 2)"
  # area:eval is free again and three READY tasks want it -- 1, 8 and 10.
  # Exactly one goes, and it is the lowest-numbered.
  - outcomes: {}
    expect:
      - "Dispatch(1, implementer, 2)"
```

`fixtures/scenarios/poison-task.yaml`:
```yaml
name: poison-task
proves: >-
  parent §10 poison task and parent §11 "second failure → NEEDS_HUMAN, not a
  third attempt"; harness §7 hard fail "no task exceeds 2 attempts". The final
  tick's list is the claim: it contains a dispatch, and it does not contain a
  third Dispatch(5, ...).
seed:
  ledger: fixtures/ledger/issues.json
  policy: fixtures/policy/fleet-fixture
ticks:
  - outcomes: {}
    expect:
      - "Dispatch(1, implementer, 1)"
      - "Dispatch(2, implementer, 1)"
      - "Dispatch(4, implementer, 1)"
      - "Dispatch(5, implementer, 1)"
      - "Dispatch(6, implementer, 1)"
  - outcomes: {5: dirty_exit}
    expect:
      - "Release(5, dirty_exit, 2)"
  # Re-dispatched at attempt 2, read back off the fleet:attempt-2 label the
  # release wrote.
  - outcomes: {}
    expect:
      - "Dispatch(5, implementer, 2)"
  # attempts == max_attempts, so the release is refused and a human is woken.
  - outcomes: {5: dirty_exit}
    expect:
      - "Escalate(5, attempts_exhausted)"
  # #5 is now fleet:human and area:data is free, so #11 -- the other data task --
  # takes the slot. No third attempt on #5 appears here or anywhere after.
  - outcomes: {}
    expect:
      - "Dispatch(11, implementer, 1)"
```

`fixtures/scenarios/stall-releases.yaml`:
```yaml
name: stall-releases
proves: >-
  parent §10 silent stall and parent §11 "stalled agent → released, attempt
  incremented". Both halves are asserted: the release carries attempt 2, and the
  re-dispatch a tick later carries it again, read back off the label.
seed:
  ledger: fixtures/ledger/issues.json
  policy: fixtures/policy/fleet-fixture
ticks:
  - outcomes: {}
    expect:
      - "Dispatch(1, implementer, 1)"
      - "Dispatch(2, implementer, 1)"
      - "Dispatch(4, implementer, 1)"
      - "Dispatch(5, implementer, 1)"
      - "Dispatch(6, implementer, 1)"
  # A first-seen agent has idle == 0, so a stall needs two frozen ticks. This is
  # why spec §7's five-tick example escalates one tick early: its §5 rule binds,
  # its example does not.
  - outcomes: {1: stall}
    expect: []
  - outcomes: {1: stall}
    expect:
      - "Release(1, stalled, 2)"
  - outcomes: {}
    expect:
      - "Dispatch(1, implementer, 2)"
```

`fixtures/scenarios/ledger-drift.yaml`:
```yaml
name: ledger-drift
proves: >-
  parent §10 ledger drift and parent §11 "CLAIMED task with no live agent →
  released"; herdr state is the tiebreaker. The drift arises inside the run --
  an agent vanishes under a CLAIMED task -- rather than being hand-seeded as a
  label, so the reconciliation is observed rather than assumed.
seed:
  ledger: fixtures/ledger/issues.json
  policy: fixtures/policy/fleet-fixture
ticks:
  - outcomes: {}
    expect:
      - "Dispatch(1, implementer, 1)"
      - "Dispatch(2, implementer, 1)"
      - "Dispatch(4, implementer, 1)"
      - "Dispatch(5, implementer, 1)"
      - "Dispatch(6, implementer, 1)"
  # herdr loses the agent; the ledger still says fleet:claimed. No status
  # reports this -- the agent is simply absent.
  - outcomes: {1: vanished}
    expect:
      - "Release(1, no_agent, 2)"
  - outcomes: {}
    expect:
      - "Dispatch(1, implementer, 2)"
```

`fixtures/scenarios/budget-drains.yaml`:
```yaml
name: budget-drains
proves: >-
  parent §10 runaway cost, both halves, and parent §11 "budget exhausted →
  drains, does not kill". Tick 2 is the per-task ceiling; ticks 3 and 4 are the
  fleet-wide 80% rule -- eleven READY tasks and five free slots and no dispatch,
  while the one agent still in flight is left alone and allowed to finish.
seed:
  ledger: fixtures/ledger/issues.json
  policy: fixtures/policy/fleet-fixture
ticks:
  - outcomes: {}
    expect:
      - "Dispatch(1, implementer, 1)"
      - "Dispatch(2, implementer, 1)"
      - "Dispatch(4, implementer, 1)"
      - "Dispatch(5, implementer, 1)"
      - "Dispatch(6, implementer, 1)"
  # Four agents run away at three times the per-task ceiling; #6 stays healthy.
  # 4 x 3 x per_task_usd is past 80% of fleet_usd, so the drain begins in the
  # same tick the four are released.
  - outcomes: {1: over_budget, 2: over_budget, 4: over_budget, 5: over_budget}
    expect:
      - "Release(1, over_budget, 2)"
      - "Release(2, over_budget, 2)"
      - "Release(4, over_budget, 2)"
      - "Release(5, over_budget, 2)"
      - "Drain(budget)"
  # Drain, do not kill: #6 is still running and is not touched, and the four
  # released tasks are not re-dispatched.
  - outcomes: {}
    expect:
      - "Drain(budget)"
  # In-flight work finishes during the drain rather than being discarded.
  - outcomes: {6: clean_exit}
    packets: {6: {to: review}}
    expect:
      - "Advance(6, review)"
      - "Drain(budget)"
```

`fixtures/scenarios/blocked-self-resolves.yaml`:
```yaml
name: blocked-self-resolves
proves: >-
  parent §11 "blocked task's dependency closes → requeued with no human
  involvement"; harness §7 hard fail, issue 4. Hand-written rather than seeded
  from fixtures/ledger/issues.json because the capture holds no BLOCKED task and
  no intent decide() emits can create one -- `fleet block` is an agent's CLI
  action, not an allocator output. blocked_on rides on the packet: there is no
  fleet:blocked-on-N label, and inventing one would be a label gh has never
  heard of.
seed:
  policy: fixtures/policy/fleet-fixture
  packets:
    4: {to: blocked, blocked_on: 11}
  issues:
    - {number: 4, title: "LinearGenerator should match moments", state: OPEN, assignees: [],
       labels: ["fleet:blocked", "fleet:auto-ok", "area:models", "class:patch"]}
    - {number: 11, title: "Add a reference sample generator", state: OPEN, assignees: [],
       labels: ["fleet:done", "fleet:auto-ok", "area:data", "class:patch"]}
ticks:
  # No human is woken and nothing is escalated. #4 is not dispatched in the same
  # tick it is unblocked -- it is still fleet:blocked when DISPATCH runs.
  - outcomes: {}
    expect:
      - "Unblock(4)"
  - outcomes: {}
    expect:
      - "Dispatch(4, implementer, 1)"
```

`fixtures/scenarios/integration-lock.yaml`:
```yaml
name: integration-lock
proves: >-
  harness amendment 2: while a class:integration task is live the allocator
  dispatches nothing new, in any area. Hand-written because
  fixtures/ledger/issues.json contains no class:integration task -- the fixture
  repo has never run an integration. Tick 1 is the positive control, tick 3 is
  the lock with two free areas and five free slots, tick 5 is dispatch resuming
  once the integrator finishes.
seed:
  policy: fixtures/policy/fleet-fixture
  issues:
    - {number: 1, title: "estimate_lid crashes for small samples", state: OPEN, assignees: [],
       labels: ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"]}
    - {number: 2, title: "train() history is a random walk", state: OPEN, assignees: [],
       labels: ["fleet:ready", "fleet:auto-ok", "area:train", "class:patch"]}
    - {number: 20, title: "integrate the reporting epic", state: OPEN, assignees: [],
       labels: ["fleet:ready", "fleet:auto-ok", "class:integration"]}
ticks:
  # The lock is on tasks already live, so all three start together. An
  # integration task carries no area and holds none.
  - outcomes: {}
    expect:
      - "Dispatch(1, implementer, 1)"
      - "Dispatch(2, implementer, 1)"
      - "Dispatch(20, integrator, 1)"
  # Reaping is not suppressed by the lock; dispatching is.
  - outcomes: {1: dirty_exit, 2: dirty_exit}
    expect:
      - "Release(1, dirty_exit, 2)"
      - "Release(2, dirty_exit, 2)"
  # The load-bearing tick: #1 and #2 are READY again, area:eval and area:train
  # are free, five slots are free, and nothing is dispatched -- because the
  # integrator touches every area by definition and per-area exclusion does not
  # cover it.
  - outcomes: {}
    expect: []
  - outcomes: {20: clean_exit}
    packets: {20: {to: done}}
    expect:
      - "Advance(20, done)"
  # The lock lifts and both areas resume, at the attempt each task had reached.
  - outcomes: {}
    expect:
      - "Dispatch(1, implementer, 2)"
      - "Dispatch(2, implementer, 2)"
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_scenarios.py -v`
Expected: PASS, 22 tests (7 scenarios × 3 + the set guard)

If a tick fails, read the message before editing the file: it prints both lists
in the scenario's own notation. A mismatch of one `Dispatch` on a captured-ledger
tick usually means a rule changed, not that the expectation was written wrong —
these five lists are what `decide()` emits against those thirteen rows, and the
exhaustive comparison is the point.

- [ ] **Step 5: Commit**

```bash
git add fixtures/scenarios tests/test_scenarios.py
git commit -m "$(cat <<'EOF'
test(scenarios): one file per failure-table row, seeded from the capture

Five of the seven seed from fixtures/ledger/issues.json, as spec §7 requires.
Hand-written rows had drifted from the capture they stood in for -- issue 1
carried three different label sets across three files and issue 20 did not exist
-- which is the drift seeding from the capture exists to prevent. The two
hand-written files say in their own `proves` why the capture cannot express
their case: nothing in it is blocked, and no integration has ever run.

Every expectation carries its attempt number, so the "attempt += 1" half of the
stall row is asserted rather than assumed, and the re-dispatch a tick later
asserts it again from the ledger side.

The budget scenario reaches the fleet-wide 80% drain, which no scenario
previously did: it shows in-flight work left alone and finishing while nothing
new is dispatched. Ledger drift arises inside the run from a vanished agent
rather than a hand-seeded fleet:claimed label, and the integration lock now has
a positive control, a tick where two free areas and five free slots dispatch
nothing, and a tick where dispatch resumes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 16: `fleet plan --dry-run`

The command body is the one place `ledger.read`, `policy.load` and `decide()` are
wired together, and inside a click command that builds a `RealGh` it is reachable
only by a run that talks to GitHub — which is to say by no test at all. It comes
out into `do_plan(cfg, gh, now)`, which `FakeGh` can drive offline. No test in
this task constructs `RealGh`.

`agents=[]` stands: M2 has no agent source, by design (spec §12 Q2/Q3). But the
consequence is not cosmetic — with no agents every `fleet:claimed` task reaps as
ledger drift, so against a fleet that is actually running the command prints a
`Release` for every live task. It prints a caveat saying so, and only when there
is a claimed task to be wrong about, because a caveat on every invocation is
noise and noise is not read.

`ReadOnlyGh` denies by default rather than refusing a list of known writes.
`close_issue` exists on `FakeGh`, on no Protocol, and mutates; a wrapper that
enumerates writes lets the next one of those through, and the test that is
supposed to catch it only ever looked at the Protocol.

**Files:**
- Modify: `src/fleet/cli.py`
- Test: `tests/test_plan.py`

**Interfaces:**
- Consumes: `decide`, `policy.load`, `ledger.read`, `simulate.render_intent`, `RealGh`
- Produces: `fleet plan [--dry-run/--no-dry-run]`, `cli.do_plan(cfg, gh, now) -> list[str]`, and `cli.ReadOnlyGh` — a wrapper that refuses every method outside an explicit read allowlist.

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import FIXTURE_POLICY
from fleet.cli import ReadOnlyGh, do_plan
from fleet.config import load_config
from fleet.gh import FakeGh, GhClient, RealGh

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)

READY = ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"]
CLAIMED = ["fleet:claimed", "fleet:auto-ok", "fleet:attempt-1", "area:eval", "class:patch"]


def public_methods(cls) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_")}


@pytest.fixture()
def policy_cfg():
    """conftest's `cfg` builds a tmp repo with schemas and no policy tables;
    policy.load needs ownership.md and task-classes.md, so this is the vendored
    fixture layer itself."""
    return load_config(FIXTURE_POLICY)


@pytest.fixture()
def gh():
    client = FakeGh()
    client.create_issue("t", "b", READY)
    return client


def test_every_public_method_outside_the_read_allowlist_is_refused(gh):
    """Enumerated from the clients, not listed by hand. `close_issue` is on
    FakeGh, on no Protocol, and mutates -- a wrapper that names its writes lets
    the next one of those through, and the previous test only read the Protocol
    so it would not have noticed."""
    surface = public_methods(GhClient) | public_methods(RealGh) | public_methods(FakeGh)
    refused = sorted(surface - ReadOnlyGh.READS)
    assert "close_issue" in refused  # the enumeration reaches past the Protocol
    for name in refused:
        with pytest.raises(PermissionError, match="dry-run"):
            getattr(ReadOnlyGh(gh), name)()


def test_reads_still_work(gh):
    guarded = ReadOnlyGh(gh)
    assert guarded.list_issues("all")[0]["number"] == 1
    assert guarded.get_issue(1)["title"] == "t"


def test_the_ledger_is_unchanged_after_a_refused_write(gh):
    """"Refused" has to mean nothing happened, not that an exception followed
    the write."""
    guarded = ReadOnlyGh(gh)
    with pytest.raises(PermissionError):
        guarded.comment(1, "hi")
    assert gh.get_issue(1)["comments"] == []


def test_the_plan_renders_intents_in_the_scenarios_own_notation(policy_cfg, gh):
    """`f"{type(intent).__name__}: {intent}"` printed `Dispatch: Dispatch(task=1,
    role='implementer', ...)` -- the class name twice, in a notation nothing else
    in the project uses. render_intent is the one the scenarios are written in."""
    assert do_plan(policy_cfg, ReadOnlyGh(gh), T0) == ["Dispatch(1, implementer, 1)"]


def test_a_claimed_task_gets_the_caveat_about_having_no_agents(policy_cfg):
    """M2 has no agent source, so every CLAIMED task reaps as ledger drift.
    Printing that Release without saying so tells an operator whose fleet is
    running to release every task it is working on."""
    client = FakeGh()
    client.create_issue("t", "b", CLAIMED)
    lines = do_plan(policy_cfg, ReadOnlyGh(client), T0)
    assert "no live agents" in lines[0]
    assert "Release(1, no_agent, 2)" in lines


def test_the_caveat_is_absent_when_nothing_is_claimed(policy_cfg, gh):
    """Printed unconditionally it is decoration, and decoration is skipped."""
    assert all("no live agents" not in line for line in do_plan(policy_cfg, ReadOnlyGh(gh), T0))


def test_an_idle_ledger_says_so(policy_cfg):
    assert do_plan(policy_cfg, ReadOnlyGh(FakeGh()), T0) == ["no action"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_plan.py -v`
Expected: FAIL — `ImportError: cannot import name 'ReadOnlyGh'`

- [ ] **Step 3: Implement**

In `src/fleet/cli.py`, extend the existing top-level import block — module-level,
like every other import in the file; a function-local import here would be the
only one in the module and E402 is not the reason, consistency is:

```python
from fleet import git, labels
from fleet import ledger as ledger_module
from fleet import policy as policy_module
from fleet.commands import do_block, do_discover, do_escalate, do_handoff, do_verdict
from fleet.config import load_config
from fleet.decide import decide
from fleet.gh import RealGh
from fleet.model import FleetState
from fleet.simulate import render_intent
```

(`do_verdict` is Task 6's addition to that line; the rest are this task's.)

Add `ReadOnlyGh` and the caveat above `_context`:

```python
# Named here rather than inlined so the test asserts the same string the command
# prints.
NO_AGENTS_CAVEAT = (
    "note: M2 has no agent source, so this tick ran with no live agents. Every "
    "fleet:claimed task therefore reads as ledger drift, and the Release below "
    "is an artefact of that rather than a recommendation."
)


class ReadOnlyGh:
    """Wraps a GhClient and refuses everything that is not a known read.

    Deny by default, not a list of known writes. `close_issue` exists on FakeGh,
    on no Protocol, and mutates -- and a wrapper that enumerates its writes
    passes the next such method straight through to the client. `--dry-run
    writes nothing` is then a property of the code path rather than of having
    read it carefully, which is the same reasoning that made FakeGh refuse
    unknown labels: a stand-in more permissive than the real thing is how M1's
    defects survived the entire offline suite.
    """

    READS = frozenset({"get_issue", "list_issues", "search_titles"})

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        if name in self.READS:
            return getattr(self._inner, name)

        def refuse(*_args, **_kwargs):
            raise PermissionError(f"dry-run: refusing to call {name}")

        return refuse


def do_plan(cfg, gh, now: datetime) -> list[str]:
    """One tick of decide() against a live ledger, rendered.

    Separate from the command because inside it -- behind `RealGh(cfg.repo_slug)`
    -- the only wiring of ledger.read, policy.load and decide() in the CLI is
    reachable by no test that does not talk to GitHub.
    """
    tasks = ledger_module.read(gh.list_issues("open"))
    intents, _ = decide(FleetState(), tasks, policy_module.load(cfg), [], now)
    lines: list[str] = []
    if any(task.state == "claimed" for task in tasks):
        lines.append(NO_AGENTS_CAVEAT)
    lines += [render_intent(intent) for intent in intents]
    return lines or ["no action"]
```

and add the command at the end of the file, after `discover` and before the
`if __name__` block:

```python
@main.command()
@click.option("--dry-run/--no-dry-run", default=True, help="Print the plan; write nothing.")
def plan(dry_run):
    """Print what the allocator would do right now."""
    if not dry_run:
        # Refused before load_config, or `fleet plan --no-dry-run` outside a
        # fleet repo complains about a missing fleet.toml instead of saying
        # which half of the flag is unimplemented.
        raise click.ClickException("only --dry-run is implemented; the daemon lands in M3")
    cfg = load_config()
    gh = ReadOnlyGh(RealGh(cfg.repo_slug))
    for line in do_plan(cfg, gh, datetime.now(timezone.utc)):
        click.echo(line)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_plan.py -v && uv run pytest && uv run ruff check .`
Expected: PASS, 7 tests, full suite green, ruff clean

- [ ] **Step 5: Verify against the live ledger by hand**

Capture the before state first — "identical to before" is not checkable without
it:

```bash
gh issue list --repo FibonAdithya/fleet-fixture --state all --limit 200 \
  --json number,labels --jq '.[] | "\(.number) \([.labels[].name] | join(","))"' > /tmp/before.txt
cd ~/TIG/fleet-fixture && uv run --project ~/TIG/fleet fleet plan --dry-run
```

Expected: `Dispatch(N, role, 1)` lines for the ready backlog — six of them
against the current fixture, one per area, since nothing is claimed there and
`list_issues("open")` does not return the closed reviewing task. Then:

```bash
gh issue list --repo FibonAdithya/fleet-fixture --state all --limit 200 \
  --json number,labels --jq '.[] | "\(.number) \([.labels[].name] | join(","))"' | diff /tmp/before.txt -
```

Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add src/fleet/cli.py tests/test_plan.py
git commit -m "$(cat <<'EOF'
feat(cli): fleet plan --dry-run

Reads the live ledger, runs one tick, prints the intents, writes nothing.

The body is `do_plan(cfg, gh, now)` so a test can drive it with FakeGh. Left
inside the command, behind RealGh, the CLI's only wiring of ledger.read,
policy.load and decide() was reachable by no offline test at all.

"Writes nothing" is enforced by a wrapper that denies by default rather than
refusing a list of known writes, and the test enumerates the public surface of
GhClient, RealGh and FakeGh instead of listing methods by hand. close_issue is
exactly the case that motivates both: it mutates, it is on FakeGh only, and the
previous test looked at the Protocol.

M2 has no agent source, so the tick runs with no live agents and every CLAIMED
task reads as ledger drift. The command says so when the ledger holds one,
rather than quietly recommending that a running fleet release everything.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 17: Update the runbook and the M2 inputs ledger

Two of these edits are corrections to claims the plan already made about itself.
The self-review said epic fan-in was "recorded in Task 17's doc update as an M3
item" and no step wrote that line. And `m2-inputs` §3's rubric-04 item — the one
Task 13 cites as its whole reason for existing — was left unstruck while five
weaker items were struck.

The rest is the record of what this milestone could not honestly prove. A
failure-table row with no scenario is not a defect; a failure-table row with no
scenario and no note is, because the next reader counts seven files against
seven claims and stops looking.

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/m2-inputs.md`

**Interfaces:**
- Consumes: everything
- Produces: documentation only

- [ ] **Step 1: Add the new commands to the runbook**

In `docs/runbook.md`, change the heading "The four write commands" to "The five
write commands" and add to that block:

```
    fleet verdict  --task N --verdict approved --head-sha SHA --checks-rerun "make check: pass"
```

Add two new sections before "Known warts":

```markdown
## Seeing what the allocator would do

    cd ~/TIG/fleet-fixture && fleet plan --dry-run

Reads the ledger, runs one tick of `decide()`, prints the intents and writes
nothing. The client is wrapped so that every method outside a read allowlist
raises — deny by default, so a mutating method added later is refused until
someone classifies it. The guarantee is a property of the code path, not of
having read it carefully.

The tick runs with **no live agents**, because M2 has no agent source. When the
ledger holds a `fleet:claimed` task the output says so above the intents: with no
agents that task is indistinguishable from ledger drift, so the
`Release(N, no_agent, 2)` printed for it is an artefact of the dry run rather
than a recommendation. Against an idle backlog the output is exactly the
dispatches the allocator would make.

`--no-dry-run` is refused until the daemon lands in M3.

## Running the scenarios

    uv run pytest tests/test_scenarios.py -v

`fixtures/scenarios/` holds one file per claim, and each names its claim in its
`proves` field. Five of the seven seed from the captured ledger
(`fixtures/ledger/issues.json`), so they are exercised against real gh output
rather than invented rows; the two that do not say in their own `proves` why the
capture cannot express their case. Every tick lists **every** intent that tick
emits — the comparison is exhaustive, which is what lets a scenario assert an
absence without writing `expect: []`.

Three rows of the design's failure table are covered by no scenario at all.
`docs/m2-inputs.md` §7 names them and says why.
```

Update the "Agent environment" section to note that `FLEET_ROLE` is now
load-bearing:

```markdown
`FLEET_ROLE` is no longer only attribution: a handoff's target state is checked
against it. An implementer may hand to `review` and nothing else; only an
integrator may reach `done`; a reviewer leaves REVIEW by writing a verdict and
cannot hand out of it at all. An unrecognised role is refused rather than
permitted, so a typo is an error and not a privilege escalation — and so is an
*unset* `FLEET_ROLE`, which used to fall back to `implementer` silently and so
handed out exactly the privilege this check exists to fence off.
```

- [ ] **Step 2: Strike the resolved items from `docs/m2-inputs.md`**

Mark these resolved, with the task that resolved them — do not delete the text,
since the file is a record of what executing the milestone found:

- §1 "Add `fleet verdict`" → **resolved, M2 Task 6**
- §2 "The allocator must gate on paths, not areas" → **resolved, M2 Task 10** — `decide()` gates on the `fleet:auto-ok` label alone; path enforcement is the scout's and the integrator's
- §3 "Rubric 04 asserts a label that will not be there at grade time" → **mechanism resolved, M2 Task 13** — the run log records every intent as it is applied, so `Unblock(4)` survives the transition that removes `fleet:blocked`. The rubric still has to be rewritten to read the log instead of the final ledger
- §5 "Nothing binds a handoff's target state to the caller's role" → **resolved, M2 Task 7**
- §5 "The fleet suite depends on a hardcoded absolute path" → **resolved, M2 Task 1**
- §5 "`_post` can leave an issue with no state label" → **resolved, M2 Tasks 6 and 11** — `expand()` emits one `SetLabels` carrying both halves, so nothing the allocator writes has the window, and Task 6 moves the CLI's own label changes through `_move_state`, which carries a state and its counter label in one `add_labels` call so a counter cannot land without its state

Add a line at the top of the file:

```markdown
> Items resolved by M2 are marked inline. The rest carry forward to M3.
```

- [ ] **Step 3: Record what M2 deliberately did not cover**

Append to `docs/m2-inputs.md`:

```markdown
## 7. What M2 deliberately did not cover

Each of these is a claim the design asks for and M2 cannot honestly make. They
are written down rather than left to be rediscovered by whoever counts seven
scenario files against ten claims.

- **Conflict storm** (parent §10). `FakeExecutor` scripted a `conflict` outcome
  that arrived at `decide()` as a dirty exit — same status, same everything — so
  no assertion could distinguish the two and a conflict scenario would have
  proved only what the dirty-exit scenario already proves. The outcome was
  removed rather than kept as coverage that is not coverage. The row needs an
  executor that can report a reason: `HerdrExecutor`, in M3.
- **Orphaned worktree** (parent §10). There are no worktrees and no reaper in
  M2; `HerdrExecutor` is an explicit spec §1 non-goal.
- **Duplicate discoveries** (parent §10). Not an allocator rule at all —
  `fleet discover` searches existing issues first, and `tests/test_discover.py`
  covers it. `decide()` never sees the case.
- **Epic fan-in** (parent §11, "last epic child closes → integration task
  auto-created"). `CreateIntegrationTask` and `CreateIssue` were defined in
  `model.py` and emitted by nothing, which forces either a dead branch in
  `expand()` or an unhandled member of the `Intent` union; both types were
  removed. Fan-in needs `parent_epic` populated and nothing writes it —
  `discover` cannot file a child with a class or a parent link (§5 above).
  **M3 re-adds the two intents if it wants them.** Carrying unused types forward
  would only make the gap look closed.
- **The REVIEW exit is unit-tested, not scenario-tested.** `fleet verdict`
  performs the transition, not `decide()`: approved → `fleet:integrating`,
  `changes_required` → `fleet:ready` plus `fleet:fix-pass-N`, or `fleet:human`
  once the cap is spent. Spec §5 ruling 4 is therefore covered by
  `tests/test_commands.py`, and no scenario drives an approval or a fix pass,
  because a scenario replays `decide()` and never runs a CLI command. The
  consequence a scenario author has to know: a task that reaches `fleet:review`
  during a simulated night stays there, holding its area and its slot, because
  nothing in the simulation can issue the verdict that would move it.
- **`slots` binds in no scenario independently of area exclusion.** The captured
  ledger's first tick dispatches five tasks with five free areas and five free
  slots, so either rule alone explains the number.
  `tests/test_decide_dispatch.py` pins the bound directly; a scenario cannot,
  because nothing lets a scenario override a policy field.
- **The `investigation` route drops a prose constraint.** `task-classes.md`
  writes that route as ``implementer, forbidden from writing `src/` ``.
  `parse_routes` takes the leading role token and discards the rest: the
  constraint is a role-prompt rule, not an allocator input, and a route
  resolving to a "role" named ``implementer, forbidden from writing `src/` ``
  is a role no `roles_dir` contains. Enforcing it stays with `implementer.md`
  and the reviewer, which is where it already was.
```

- [ ] **Step 4: Run the full check**

Run: `make check`
Expected: all green

- [ ] **Step 5: Commit**

```bash
git add docs/runbook.md docs/m2-inputs.md
git commit -m "$(cat <<'EOF'
docs: record what M2 resolved, and what it could not

Marks resolved items inline in m2-inputs rather than deleting them -- the file
is a record of what executing the milestone found, and a struck item is more
useful than an absent one. Rubric 04 is struck as a mechanism: Task 13's run log
is what can answer "was fleet:blocked ever applied", and the rubric still has to
be rewritten to read it.

Adds §7, the coverage M2 does not have. A failure-table row with no scenario is
a decision; a failure-table row with no scenario and no note is a hole, because
the next reader counts seven files against ten claims and stops looking. Conflict
storm, orphaned worktrees, duplicate discoveries, epic fan-in, the REVIEW exit
and the investigation route's dropped prose constraint each say why.

Notes in the runbook that FLEET_ROLE is now load-bearing rather than
attribution, unset included, and that `fleet plan --dry-run` runs its tick with
no live agents -- so a claimed task in the ledger reads as drift and the Release
printed for it is an artefact rather than advice.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §2 three CLI debt items | 2 (budget), 6 (verdict), 7 (role binding) |
| §2 vendored policy by-product | 1 |
| §3 module decomposition | 3, 4, 5, 8–14 |
| §3 `ledger.py` parity choke point | 4 |
| §3 `FleetState` derived and disposable | 3, 8 |
| §3 `expand.py` pure and separate | 11 |
| §4 purity seam / the fold | 3, 8 |
| §5 REAP | 8 |
| §5 UNBLOCK | 9 |
| §5 DISPATCH, all four rulings | 10 |
| §5 the run log | 13 |
| §6 executor seam | 12 |
| §7 scenarios | 14, 15 |
| §7 vendored policy fixture | 1 |
| §8 `fleet plan --dry-run` | 16 |
| §9 strict policy parsing | 5 |
| §10 testing obligations | 4 (parity), 11 (expand), 15 (failure table), 16 (dry-run), 5 (policy) |

**Gaps found and closed during review:**

- Spec §5 mentions `CreateIntegrationTask` (epic fan-in) as an intent. Nothing emits it, and no scenario covers parent §11's "last epic child closes → integration task auto-created". **This is a known, deliberate gap: fan-in needs `parent_epic` populated on `Task`, and nothing writes that label today** — `discover` cannot file a child with a class or a parent link (`m2-inputs` §5). `CreateIntegrationTask` and `CreateIssue` were therefore removed from `model.py` rather than defined and left unemitted, which would force either a dead branch in `expand()` or an unhandled member of the `Intent` union. M3 re-adds them if it wants them. Recorded in Task 17's doc update as an M3 item.
- Task 15's `blocked-self-resolves` scenario needs `blocked_on`, which comes from a packet rather than a label. `packets` is therefore a first-class key of the scenario seed in Task 14, not an extension left to be discovered — without it `clean_exit` can never produce an `Advance` and UNBLOCK can never fire at all.
- `simulate` no longer reaches into `ledger`'s private names: Task 14 carries its own `_label_names` helper instead, so the label-shape knowledge the fold needs is stated where it is used rather than borrowed across a module boundary.

**Type consistency:** `decide()`'s signature is fixed in Task 8 and unchanged by Tasks 9 and 10. `expand(intent, policy)` keeps the spec's two-argument shape: the attempt number rides on the intent itself (`Dispatch(task, role, base, attempt)`, `Release(task, reason, attempt)`) rather than arriving as a keyword, so the counter has exactly one writer. `Task.klass` (not `class_`) is used consistently from Task 3 onward. `FakeExecutor.script/tick/apply/agents/packets` match between Tasks 12, 14 and 15.

---

## Known scope boundaries

Carried to M3+, unchanged from `docs/m2-inputs.md`:

- Epic fan-out / fan-in (no intent models it; M2 removed the two unemitted types rather than carrying them)
- The fixture's single-use seeder; a graded re-run is not yet possible
- The grader's 14 manual assertions and 8 human-needed rubrics
- Issue 5's escapable poison and issue 1/8's non-conflicting conflict pair
- `discover` cannot file a child with a class or acceptance criteria
- No shared `FleetError` base; rejected commands still print a traceback
- `close_issue` exists on `FakeGh` only and is absent from the `GhClient` Protocol
