# M2: Allocator Pure Core and Simulator — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `decide()` — a pure fold from ledger, policy and agent states to allocation intents — plus a scripted fake executor, a YAML scenario harness that replays whole nights offline, and `fleet plan --dry-run`.

**Architecture:** Every impure edge (gh, herdr, the clock, the filesystem) is normalized by a thin shell into frozen dataclasses. `decide()` threads its own derived memory explicitly so cross-tick bookkeeping — attempt counters, fix-pass counters, stall timers — is unit-testable. Intents describe policy; a separate pure `expand()` turns each into concrete effects, keeping `decide()`'s tests readable while still covering the label-mutation sequence.

**Tech Stack:** Python 3.10+, `click`, `jsonschema`, `PyYAML`, `tomli`, `pytest`, `ruff`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-17-m2-allocator-design.md`

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
- Create: `fixtures/policy/README.md`
- Modify: `tests/conftest.py:22`

**Interfaces:**
- Consumes: nothing
- Produces: `tests/conftest.py` exposes `FIXTURE_POLICY: Path` (the vendored `fleet-fixture` root) and `FIXTURE_SCHEMAS: Path` (`FIXTURE_POLICY / ".agents/schemas"`). Every later task's `cfg` fixture resolves through these.

- [ ] **Step 1: Copy the policy layer out of the fixture repo**

```bash
cd ~/TIG/fleet
mkdir -p fixtures/policy/fleet-fixture/docs/agent
mkdir -p fixtures/policy/fleet-fixture/.agents/schemas
cp ~/TIG/fleet-fixture/fleet.toml                      fixtures/policy/fleet-fixture/
cp ~/TIG/fleet-fixture/docs/agent/ownership.md         fixtures/policy/fleet-fixture/docs/agent/
cp ~/TIG/fleet-fixture/docs/agent/task-classes.md      fixtures/policy/fleet-fixture/docs/agent/
cp ~/TIG/fleet-fixture/.agents/schemas/*.json          fixtures/policy/fleet-fixture/.agents/schemas/
```

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

In `load_config`, change the unpacking line and add the two fields:

```python
    repo, policy, fleet = data["repo"], data["policy"], data.get("fleet", {})
    budget = data.get("budget", {})
```

and inside the `FleetConfig(...)` call, after `max_fix_passes=...`:

```python
        per_task_usd=float(budget.get("per_task_usd", 2.0)),
        fleet_usd=float(budget.get("fleet_usd", 25.0)),
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

**Files:**
- Create: `src/fleet/model.py`
- Test: `tests/test_model.py`

**Interfaces:**
- Consumes: nothing
- Produces: every type the rest of M2 uses. Exact definitions below — later tasks import these names verbatim.

- [ ] **Step 1: Write the failing test**

Create `tests/test_model.py`:

```python
from __future__ import annotations

import dataclasses

import pytest

from fleet.model import AgentState, Dispatch, Observation, Policy, Task


def test_task_is_frozen_and_hashable():
    """Frozen with tuple collections, not lists: FakeGh handing out its live
    `labels` list was one of M1's defects, and a mutable domain type is the same
    shape -- a caller that edits what it was given corrupts a shared snapshot."""
    task = Task(number=1, title="t", state="ready", area="eval", klass="patch", auto_ok=True)
    assert hash(task)
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.state = "claimed"


def test_task_defaults_are_the_absent_case_not_the_zero_case():
    task = Task(number=1, title="t", state="triage", area=None, klass=None, auto_ok=False)
    assert task.attempts == 0
    assert task.fix_passes == 0
    assert task.packet is None
    assert task.verdict is None
    assert task.blocked_on is None


def test_intents_compare_by_value():
    """decide()'s tests assert on intent equality, so value semantics are the
    contract, not an implementation detail."""
    assert Dispatch(task=1, role="implementer", base="integration") == Dispatch(
        task=1, role="implementer", base="integration"
    )


def test_policy_routes_are_tuples():
    policy = Policy(
        areas=(("src/eval/**", "eval"),),
        routes=(("patch", ("implementer", "reviewer", "integrator")),),
        slots=6,
        stall_minutes=10,
        max_attempts=2,
        max_fix_passes=1,
        per_task_usd=2.0,
        fleet_usd=25.0,
    )
    assert policy.route_for("patch") == ("implementer", "reviewer", "integrator")
    assert policy.route_for("nonexistent") == ()


def test_agent_state_carries_what_a_snapshot_cannot():
    """herdr's snapshot has no timestamp and no issue number -- only
    agent_status, state_change_seq and a worktree cwd. The shell supplies both
    missing facts; this type is where they arrive."""
    state = AgentState(task=5, status="working", change_seq=551, cost_usd=0.4)
    assert state.task == 5


def test_observation_records_when_the_sequence_last_moved():
    from datetime import datetime, timezone

    obs = Observation(change_seq=551, since=datetime(2026, 8, 17, tzinfo=timezone.utc))
    assert obs.change_seq == 551
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

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Task:
    """One GitHub issue, as the allocator sees it.

    `packet` and `verdict` are populated only for tasks whose state carries one,
    because `gh issue list` does not fetch comment bodies and FakeGh mirrors
    that. `None` here is a fact about the query, not about the issue.
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
    packet: dict | None = None
    verdict: dict | None = None


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
    areas: tuple[tuple[str, str], ...]  # (glob, area)
    routes: tuple[tuple[str, tuple[str, ...]], ...]  # (class, role sequence)
    slots: int
    stall_minutes: int
    max_attempts: int
    max_fix_passes: int
    per_task_usd: float
    fleet_usd: float

    def route_for(self, klass: str | None) -> tuple[str, ...]:
        return dict(self.routes).get(klass or "", ())


# ---------------------------------------------------------------- intents


@dataclass(frozen=True)
class Dispatch:
    task: int
    role: str
    base: str


@dataclass(frozen=True)
class Release:
    task: int
    reason: str


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
class CreateIntegrationTask:
    epic: int


@dataclass(frozen=True)
class Drain:
    reason: str


Intent = Dispatch | Release | Escalate | Unblock | Advance | CreateIntegrationTask | Drain


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


@dataclass(frozen=True)
class CreateIssue:
    title: str
    body: str
    labels: tuple[str, ...]


Effect = SetLabels | Assign | StartAgent | StopAgent | CreateIssue
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_model.py -v && uv run ruff check src/fleet/model.py`
Expected: PASS, and ruff clean

- [ ] **Step 5: Commit**

```bash
git add src/fleet/model.py tests/test_model.py
git commit -m "$(cat <<'EOF'
feat(model): frozen domain types for the allocator

Tuples rather than lists inside every dataclass. FakeGh handing out its live
`labels` list was an M1 defect that passed the whole offline suite and would
have done nothing against GitHub; a mutable domain type is the same shape.

FleetState carries only what one snapshot cannot answer -- idle timers and the
sequence counters behind stall detection. It holds no authority, so losing it
on restart costs a late stall detection and nothing else.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `ledger.py` — the parity choke point

**Files:**
- Create: `src/fleet/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Consumes: `Task` from Task 3; `labels.STATES`, `labels.AREAS`, `labels.CLASSES` (existing)
- Produces: `read(rows: list[dict]) -> list[Task]` and `read_one(row: dict) -> Task`. Accepts both gh's label-object shape and `FakeGh`'s label-name shape.

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
    """gh returns [{"id":..,"name":..}]; FakeGh stores ["fleet:ready"]. This is
    the divergence fixtures/ledger/README.md warns about, and ledger.read is the
    one place that has to absorb it."""
    as_objects = row(labels=[{"id": "x", "name": "fleet:ready"}, {"id": "y", "name": "area:eval"}])
    as_names = row(labels=["fleet:ready", "area:eval"])
    assert ledger.read_one(as_objects) == ledger.read_one(as_names)


def test_state_is_read_from_the_label_not_the_issue_state():
    """`state` in gh's JSON is OPEN/CLOSED -- the issue's openness. The fleet
    state is a label. Conflating them makes every open task look "ready"."""
    task = ledger.read_one(row(labels=["fleet:claimed"], state="OPEN"))
    assert task.state == "claimed"
    assert task.is_open is True


def test_closed_is_matched_case_insensitively():
    assert ledger.read_one(row(state="CLOSED")).is_open is False
    assert ledger.read_one(row(state="closed")).is_open is False


def test_a_task_with_no_state_label_reads_as_triage():
    """_post removes all eight state labels then adds one. A crash between the
    two leaves a task label-less, and the allocator must see it rather than
    KeyError on it."""
    assert ledger.read_one(row(labels=["area:eval"])).state == "triage"


def test_counters_take_the_highest_label_present():
    task = ledger.read_one(row(labels=["fleet:attempt-1", "fleet:attempt-2"]))
    assert task.attempts == 2


def test_counters_are_zero_when_absent():
    task = ledger.read_one(row(labels=["fleet:ready"]))
    assert task.attempts == 0
    assert task.fix_passes == 0


def test_auto_ok_is_read_from_its_own_label():
    assert ledger.read_one(row(labels=["fleet:auto-ok"])).auto_ok is True
    assert ledger.read_one(row(labels=["area:configs"])).auto_ok is False


def test_area_and_class_are_read_from_their_prefixes():
    task = ledger.read_one(row(labels=["area:configs", "class:investigation"]))
    assert task.area == "configs"
    assert task.klass == "investigation"


def test_an_unknown_area_label_is_rejected_loudly():
    """A typo'd area silently becomes an area no ownership rule covers, so the
    task is excluded from nothing and every agent piles into it."""
    with pytest.raises(ledger.LedgerError, match="unknown area"):
        ledger.read_one(row(labels=["area:evla"]))


def test_the_captured_real_ledger_parses():
    """The whole point of capturing it. If this ever fails, the fake and the
    real client have diverged again."""
    tasks = ledger.read(json.loads(CAPTURED.read_text()))
    assert len(tasks) == 13
    by_number = {t.number: t for t in tasks}
    assert by_number[1].state == "ready"
    assert by_number[1].area == "eval"
    assert by_number[1].klass == "patch"
    assert by_number[1].auto_ok is True
    assert by_number[12].is_open is False


def test_fake_gh_output_parses_the_same_way():
    """Parity: the same assertions over the fake's shape."""
    gh = FakeGh()
    gh.create_issue("t", "b", ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"])
    task = ledger.read(gh.list_issues("all"))[0]
    assert (task.state, task.area, task.klass, task.auto_ok) == ("ready", "eval", "patch", True)


def test_blocked_on_comes_from_the_packet_not_a_label():
    """There is no fleet:blocked-on-N label, and inventing one would be a label
    gh has never heard of. The dependency lives in the handoff packet."""
    task = ledger.read_one(row(labels=["fleet:blocked"]), packet={"to": "blocked", "blocked_on": 11})
    assert task.blocked_on == 11
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
    values = [n[len(prefix) :] for n in names if n.startswith(prefix)]
    if not values:
        return None
    for value in values:
        if value not in allowed:
            raise LedgerError(f"unknown {kind}: {value!r}")
    return values[0]


def read_one(row: dict, packet: dict | None = None, verdict: dict | None = None) -> Task:
    names = _names(row.get("labels", []))
    state = _one_of(names, "fleet:", labels_module.STATES, "state")
    assignees = row.get("assignees") or []
    first = assignees[0] if assignees else None
    return Task(
        number=row["number"],
        title=row.get("title", ""),
        # No state label reads as triage rather than raising: _post removes all
        # eight then adds one, so a crash between the two is a real state the
        # allocator has to be able to see.
        state=state or "triage",
        area=_one_of(names, "area:", labels_module.AREAS, "area"),
        klass=_one_of(names, "class:", labels_module.CLASSES, "class"),
        auto_ok=labels_module.AUTO_OK in names,
        attempts=_counter(names, "fleet:attempt-"),
        fix_passes=_counter(names, "fleet:fix-pass-"),
        assignee=first.get("login") if isinstance(first, dict) else first,
        # gh returns OPEN/CLOSED; matched case-insensitively for the same reason
        # FakeGh.list_issues does it -- gh's --state flag accepts any casing.
        is_open=str(row.get("state", "OPEN")).upper() != "CLOSED",
        blocked_on=(packet or {}).get("blocked_on"),
        packet=packet,
        verdict=verdict,
    )


def read(rows: list[dict], packets: dict[int, dict] | None = None,
         verdicts: dict[int, dict] | None = None) -> list[Task]:
    packets = packets or {}
    verdicts = verdicts or {}
    return [
        read_one(row, packets.get(row["number"]), verdicts.get(row["number"])) for row in rows
    ]
```

Note: `_one_of` returns the first value but validates all of them, so a task
mislabelled with two areas raises rather than silently picking one.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ledger.py -v`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add src/fleet/ledger.py tests/test_ledger.py
git commit -m "$(cat <<'EOF'
feat(ledger): parse gh JSON into Task, from both shapes

One choke point for the divergence that produced four of M0-M1's defects: gh
returns label objects, FakeGh stores names; gh says OPEN/CLOSED, callers say
open. The parity tests assert the same properties over FakeGh output and over
the captured fixtures/ledger/issues.json.

A row with no state label reads as triage rather than raising. That is not
defensive coding -- _post removes all eight state labels then adds one, so a
crash between the two produces exactly this row, and the allocator has to see
the task rather than KeyError on it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `policy.py` — strict parsing of the policy layer

**Files:**
- Create: `src/fleet/policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `Policy` from Task 3; `FleetConfig` from Task 2
- Produces: `load(cfg: FleetConfig) -> Policy`, `parse_ownership(text: str) -> tuple[tuple[str, str], ...]`, `parse_routes(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]`, `area_for(policy, path) -> str | None`, and `PolicyError`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_policy.py`:

```python
from __future__ import annotations

import pytest

from conftest import FIXTURE_POLICY
from fleet.config import load_config
from fleet.policy import PolicyError, area_for, load, parse_ownership, parse_routes

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


def test_ownership_reads_the_glob_table():
    assert parse_ownership(OWNERSHIP) == (
        ("src/eval/**", "eval"),
        ("src/train/**", "train"),
        ("gates/**", "configs"),
    )


def test_ownership_ignores_the_second_table():
    """The file carries a Non-dispatchable table with a different header. Reading
    it as globs would map `gates/**` to the area "human judgement"."""
    areas = dict(parse_ownership(OWNERSHIP))
    assert areas["gates/**"] == "configs"


def test_an_empty_ownership_table_raises():
    """A silently-empty map means no area exclusion, so every task looks
    dispatchable and six agents run into one area -- a failure that passes every
    test not specifically looking for it."""
    with pytest.raises(PolicyError, match="no ownership rules"):
        parse_ownership("# Ownership map\n\nNothing here.\n")


def test_a_malformed_ownership_row_raises():
    with pytest.raises(PolicyError, match="unknown area"):
        parse_ownership("| Glob | Area |\n|---|---|\n| `src/x/**` | notanarea |\n")


def test_routes_split_on_the_arrow():
    assert dict(parse_routes(CLASSES))["patch"] == ("implementer", "reviewer", "integrator")
    assert dict(parse_routes(CLASSES))["spec"] == ("planner",)


def test_an_empty_class_table_raises():
    with pytest.raises(PolicyError, match="no task classes"):
        parse_routes("# Task classes\n\nNothing.\n")


def test_area_for_matches_the_most_specific_glob():
    policy_areas = (("src/**", "models"), ("src/eval/**", "eval"))
    assert area_for(policy_areas, "src/eval/lid.py") == "eval"
    assert area_for(policy_areas, "src/models/gen.py") == "models"
    assert area_for(policy_areas, "README.md") is None


def test_load_reads_the_vendored_fixture_policy():
    """End to end against the real captured policy layer, not a synthetic one."""
    cfg = load_config(FIXTURE_POLICY)
    policy = load(cfg)
    assert policy.slots == 6
    assert policy.stall_minutes == 10
    assert policy.max_attempts == 2
    assert policy.max_fix_passes == 1
    assert policy.per_task_usd == 2.0
    assert policy.fleet_usd == 25.0
    assert policy.route_for("patch") == ("implementer", "reviewer", "integrator")
    assert dict(policy.areas)["src/eval/**"] == "eval"
    assert dict(policy.areas)["gates/**"] == "configs"
```

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
from fnmatch import fnmatch

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


def parse_routes(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    rows = _rows(text, ("class", "route"))
    routes = []
    for cells in rows:
        if len(cells) < 2:
            raise PolicyError(f"malformed task-class row: {cells!r}")
        name = _unticked(cells[0])
        if name not in labels_module.CLASSES:
            raise PolicyError(f"unknown class {name!r} in task classes")
        stages = tuple(s.strip() for s in cells[1].split("→") if s.strip())
        if not stages:
            raise PolicyError(f"class {name!r} has no route")
        routes.append((name, stages))
    if not routes:
        raise PolicyError("no task classes found; nothing could ever be routed")
    return tuple(routes)


def area_for(areas: tuple[tuple[str, str], ...], path: str) -> str | None:
    """The most specific matching glob wins, measured by glob length. `src/**`
    and `src/eval/**` both match src/eval/lid.py; the longer one is the answer."""
    best: tuple[int, str] | None = None
    for glob, area in areas:
        if fnmatch(path, glob) and (best is None or len(glob) > best[0]):
            best = (len(glob), area)
    return best[1] if best else None


def load(cfg: FleetConfig) -> Policy:
    return Policy(
        areas=parse_ownership(cfg.ownership.read_text()),
        routes=parse_routes(cfg.task_classes.read_text()),
        slots=cfg.slots,
        stall_minutes=cfg.stall_minutes,
        max_attempts=cfg.max_attempts,
        max_fix_passes=cfg.max_fix_passes,
        per_task_usd=cfg.per_task_usd,
        fleet_usd=cfg.fleet_usd,
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_policy.py -v`
Expected: PASS, 9 tests

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

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `fleet verdict`

REVIEW has no legal exit today. `verdict.schema.json` exists and nothing writes it.

**Files:**
- Modify: `src/fleet/commands.py`
- Modify: `src/fleet/cli.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `validate` (existing), `render`/`parse_latest` (existing), `GhClient` (existing)
- Produces: `do_verdict(cfg, gh, *, task, verdict, head_sha, reviewer, findings, checks_rerun=None, packet_was_sufficient=None) -> dict`. `findings` is a list of `{"severity": ..., "detail": ..., "path": ...}` dicts. Moves the state label to `fleet:integrating` on `approved` and back to `fleet:claimed` on `changes_required`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_commands.py`:

```python
from fleet.commands import do_verdict


def test_an_approved_verdict_moves_the_task_to_integrating(cfg, gh):
    do_verdict(
        cfg, gh, task=1, verdict="approved", head_sha="a91c204",
        reviewer="fleet-4", findings=[], checks_rerun="make check: pass",
    )
    names = gh.get_issue(1)["labels"]
    assert "fleet:integrating" in names
    assert "fleet:claimed" not in names


def test_changes_required_sends_it_back_to_claimed(cfg, gh):
    do_verdict(
        cfg, gh, task=1, verdict="changes_required", head_sha="a91c204",
        reviewer="fleet-4",
        findings=[{"severity": "must_fix", "detail": "no test binds the fix"}],
    )
    assert "fleet:claimed" in gh.get_issue(1)["labels"]


def test_the_verdict_round_trips_through_the_comment(cfg, gh):
    do_verdict(
        cfg, gh, task=1, verdict="approved", head_sha="a91c204",
        reviewer="fleet-4", findings=[],
    )
    recovered = parse_latest(gh.get_issue(1)["comments"], "verdict")
    assert recovered["verdict"] == "approved"
    assert recovered["head_sha"] == "a91c204"


def test_a_verdict_without_a_head_sha_is_refused(cfg, gh):
    """A verdict that does not name the head it reviewed is not a gate."""
    with pytest.raises(SchemaError, match="head_sha"):
        do_verdict(
            cfg, gh, task=1, verdict="approved", head_sha="",
            reviewer="fleet-4", findings=[],
        )
    assert gh.get_issue(1)["comments"] == []


def test_changes_required_needs_at_least_one_finding(cfg, gh):
    """"Changes required" with nothing to change is a task the implementer
    cannot action, and it costs a fix pass."""
    with pytest.raises(ValueError, match="at least one finding"):
        do_verdict(
            cfg, gh, task=1, verdict="changes_required", head_sha="a91c204",
            reviewer="fleet-4", findings=[],
        )
    assert gh.get_issue(1)["comments"] == []


def test_an_unknown_verdict_value_is_refused(cfg, gh):
    with pytest.raises(SchemaError, match="verdict"):
        do_verdict(
            cfg, gh, task=1, verdict="looks good to me", head_sha="a91c204",
            reviewer="fleet-4", findings=[],
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_commands.py -k verdict -v`
Expected: FAIL — `ImportError: cannot import name 'do_verdict'`

- [ ] **Step 3: Implement**

In `src/fleet/commands.py`, add after `do_escalate`:

```python
_VERDICT_STATE = {"approved": "integrating", "changes_required": "claimed"}


def do_verdict(
    cfg: FleetConfig,
    gh: GhClient,
    *,
    task: int,
    verdict: str,
    head_sha: str,
    reviewer: str,
    findings: list[dict],
    checks_rerun: str | None = None,
    packet_was_sufficient: bool | None = None,
) -> dict:
    """Record a review verdict. This is the only legal exit from REVIEW."""
    if verdict == "changes_required" and not findings:
        # The schema allows an empty findings array, correctly -- an approval has
        # nothing to list. But "changes required" with nothing to change is a
        # task the implementer cannot action, and it costs one of the single
        # bounded fix pass. Refuse it here, where the state is known.
        raise ValueError("a changes_required verdict needs at least one finding")
    gh.get_issue(task)
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
    gh.comment(task, render(payload))
    gh.remove_labels(task, labels.all_state_labels())
    gh.add_labels(task, [labels.state_label(_VERDICT_STATE[verdict])])
    return payload
```

Note `_post` is not reused: it validates against the `handoff` schema and its
`packet["task"]` lookup assumes a handoff's shape.

In `src/fleet/cli.py`, add the command after `escalate`:

```python
@main.command()
@click.option("--task", type=int, required=True)
@click.option("--verdict", required=True, type=click.Choice(["approved", "changes_required"]))
@click.option("--head-sha", required=True, help="The head you actually reviewed.")
@click.option("--checks-rerun", default=None, help='e.g. "make check: pass"')
@click.option(
    "--finding",
    "findings",
    multiple=True,
    help="severity:detail, e.g. must_fix:no test binds the fix",
)
@click.option("--packet-was-sufficient/--packet-was-insufficient", default=None)
def verdict(task, verdict, head_sha, checks_rerun, findings, packet_was_sufficient):
    """Record a review verdict. The only legal exit from REVIEW."""
    cfg, gh, identity = _context()
    parsed = []
    for raw in findings:
        severity, _, detail = raw.partition(":")
        if not detail.strip():
            raise click.ClickException(f"finding {raw!r} has no detail; use severity:detail")
        parsed.append({"severity": severity.strip(), "detail": detail.strip()})
    do_verdict(
        cfg,
        gh,
        task=task,
        verdict=verdict,
        head_sha=head_sha,
        reviewer=identity["agent"],
        findings=parsed,
        checks_rerun=checks_rerun,
        packet_was_sufficient=packet_was_sufficient,
    )
    click.echo(f"recorded {verdict} on task {task}")
```

Add `do_verdict` to the `fleet.commands` import at the top of `cli.py`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fleet/commands.py src/fleet/cli.py tests/test_commands.py
git commit -m "$(cat <<'EOF'
feat(cli): fleet verdict -- the only legal exit from REVIEW

verdict.schema.json has existed since M0 with nothing writing it. The M1.5 trial
found this the hard way: agent B produced a correct approval that existed only
in its reply and would have evaporated in an unattended run. decide() cannot
advance a task out of REVIEW without one.

changes_required with no findings is refused here rather than in the schema. An
approval legitimately has nothing to list, so the schema cannot require a
non-empty array -- but "changes required" with nothing to change is a task the
implementer cannot action, and it spends the single bounded fix pass.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Bind a handoff's target state to the caller's role

An implementer can hand its own task to `done`, skipping review and integration. Grading reads terminal state from labels, so a self-declared `done` scores as correct.

**Files:**
- Modify: `src/fleet/commands.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `do_handoff` (existing)
- Produces: `do_handoff` gains a required keyword `role` check. `commands.ROLE_MAY_HAND_TO: dict[str, frozenset[str]]` is importable for tests and for the CLI's error text.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_commands.py`:

```python
def test_an_implementer_cannot_declare_its_own_task_done(cfg, gh):
    """Grading reads terminal state from labels, so a self-declared `done`
    scores as rubric-correct while skipping review and integration entirely."""
    with pytest.raises(ValueError, match="implementer.*may not hand.*done"):
        do_handoff(cfg, gh, task=1, to="done", **BASE)
    assert gh.get_issue(1)["comments"] == []
    assert "fleet:done" not in gh.get_issue(1)["labels"]


def test_an_implementer_may_hand_to_review(cfg, gh):
    do_handoff(cfg, gh, task=1, to="review", **BASE)
    assert "fleet:review" in gh.get_issue(1)["labels"]


def test_only_the_integrator_may_reach_done(cfg, gh):
    do_handoff(cfg, gh, task=1, to="done", **{**BASE, "role": "integrator"})
    assert "fleet:done" in gh.get_issue(1)["labels"]


def test_a_reviewer_may_not_hand_to_review(cfg, gh):
    """A reviewer handing to review is a loop with no writer -- REVIEW is
    read-only, so nothing would ever advance it."""
    with pytest.raises(ValueError, match="reviewer.*may not hand.*review"):
        do_handoff(cfg, gh, task=1, to="review", **{**BASE, "role": "reviewer"})


def test_an_unknown_role_is_refused_rather_than_permitted(cfg, gh):
    """Defaulting an unrecognised role to "allow everything" would make a typo in
    FLEET_ROLE a privilege escalation."""
    with pytest.raises(ValueError, match="unknown role"):
        do_handoff(cfg, gh, task=1, to="done", **{**BASE, "role": "superuser"})
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_commands.py -k role -v`
Expected: FAIL — no exception raised; the handoff succeeds

- [ ] **Step 3: Implement**

In `src/fleet/commands.py`, add above `do_handoff`:

```python
# Which target states each role may write. An implementer that can reach `done`
# skips review and integration, and since grading reads terminal state from
# labels, a self-declared `done` scores as rubric-correct. This is the mechanical
# half of parent §4's "give every state one writer".
ROLE_MAY_HAND_TO = {
    "implementer": frozenset({"review"}),
    "reviewer": frozenset({"claimed", "integrating"}),
    "invariant-reviewer": frozenset({"claimed", "integrating"}),
    "planner": frozenset({"review"}),
    "integrator": frozenset({"done", "claimed"}),
    "scout": frozenset(),
}
```

and at the top of `do_handoff`, after the `blocked`/`human` guard:

```python
    allowed = ROLE_MAY_HAND_TO.get(role)
    if allowed is None:
        # Not a default-allow: a typo in FLEET_ROLE would otherwise be a
        # privilege escalation rather than an error.
        raise ValueError(f"unknown role {role!r}; cannot check what it may hand to")
    if to not in allowed:
        permitted = ", ".join(sorted(allowed)) or "nothing"
        raise ValueError(f"role {role!r} may not hand to {to!r}; it may hand to {permitted}")
```

`do_handoff`'s signature changes from `(cfg, gh, *, task, to, **kw)` to
`(cfg, gh, *, task, to, role, **kw)`, and `role=role` must be passed through to
`build_handoff`:

```python
def do_handoff(cfg: FleetConfig, gh: GhClient, *, task: int, to: str, role: str, **kw) -> dict:
    ...
    return _post(cfg, gh, build_handoff(task=task, to=to, role=role, **kw), to)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_commands.py tests/test_cli.py -v`
Expected: PASS. `role` already arrives via `**identity` from `cli._context`, so the CLI needs no change.

- [ ] **Step 5: Commit**

```bash
git add src/fleet/commands.py tests/test_commands.py
git commit -m "$(cat <<'EOF'
fix(commands): bind a handoff's target state to the caller's role

An implementer could hand its own task to `done`, skipping review and
integration. The grader reads terminal state from labels, so that scored as
rubric-correct -- an agent could pass the bar by relabelling rather than by
working.

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
- Consumes: everything from Tasks 3–5
- Produces: `decide(prev: FleetState, ledger: list[Task], policy: Policy, agents: list[AgentState], now: datetime) -> tuple[list[Intent], FleetState]`. This signature is final; Tasks 9 and 10 extend the body only.

- [ ] **Step 1: Write the failing test**

Create `tests/test_decide_reap.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fleet.decide import decide
from fleet.model import (
    AgentState,
    Advance,
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
    slots=6,
    stall_minutes=10,
    max_attempts=2,
    max_fix_passes=1,
    per_task_usd=2.0,
    fleet_usd=25.0,
)


def claimed(number=1, attempts=1, **kw):
    return Task(
        number=number, title="t", state="claimed", area="eval", klass="patch",
        auto_ok=True, attempts=attempts, **kw,
    )


def only(intents, kind):
    return [i for i in intents if isinstance(i, kind)]


def test_a_clean_exit_advances_to_the_packets_target():
    task = claimed(packet={"to": "review", "head_sha": "a91c204"})
    agents = [AgentState(task=1, status="done", change_seq=10)]
    intents, _ = decide(FleetState(), [task], POLICY, agents, T0)
    assert Advance(task=1, to="review") in intents


def test_a_dirty_exit_releases_and_counts_an_attempt():
    agents = [AgentState(task=1, status="dirty", change_seq=10)]
    intents, _ = decide(FleetState(), [claimed()], POLICY, agents, T0)
    assert Release(task=1, reason="dirty_exit") in intents


def test_an_agent_whose_sequence_has_not_moved_for_the_threshold_is_stalled():
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="working", change_seq=10)]
    intents, _ = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=10))
    assert Release(task=1, reason="stalled") in intents


def test_an_agent_still_moving_is_not_stalled():
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="working", change_seq=11)]
    intents, state = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=10))
    assert only(intents, Release) == []
    assert state.observation(1).change_seq == 11
    assert state.observation(1).since == T0 + timedelta(minutes=10)


def test_an_agent_just_under_the_threshold_is_not_stalled():
    prev = FleetState(seen=((1, Observation(change_seq=10, since=T0)),))
    agents = [AgentState(task=1, status="working", change_seq=10)]
    intents, _ = decide(prev, [claimed()], POLICY, agents, T0 + timedelta(minutes=9, seconds=59))
    assert only(intents, Release) == []


def test_a_second_failure_escalates_rather_than_making_a_third_attempt():
    """Parent §4's attempt cap. Without it an unattended run spends the night
    and a large token budget re-failing one task."""
    agents = [AgentState(task=1, status="dirty", change_seq=10)]
    intents, _ = decide(FleetState(), [claimed(attempts=2)], POLICY, agents, T0)
    assert Escalate(task=1, reason="attempts_exhausted") in intents
    assert only(intents, Release) == []


def test_a_claimed_task_with_no_live_agent_is_released():
    """Ledger drift. herdr's state is the tiebreaker, per parent §10."""
    intents, _ = decide(FleetState(), [claimed()], POLICY, [], T0)
    assert Release(task=1, reason="no_agent") in intents


def test_over_per_task_budget_releases():
    agents = [AgentState(task=1, status="working", change_seq=10, cost_usd=2.5)]
    intents, _ = decide(FleetState(), [claimed()], POLICY, agents, T0)
    assert Release(task=1, reason="over_budget") in intents


def test_a_task_that_is_not_claimed_is_not_reaped():
    ready = Task(number=1, title="t", state="ready", area="eval", klass="patch", auto_ok=True)
    intents, _ = decide(FleetState(), [ready], POLICY, [], T0)
    assert only(intents, Release) == []


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
puts the attempt counter, the fix-pass counter and the stall timer under test
instead of leaving them in shell bookkeeping nothing reaches.

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

# Agent statuses that mean the process is gone. "done" is a clean finish;
# "dirty" is a non-zero exit.
_FINISHED = {"done", "dirty"}


def _observe(
    prev: FleetState, agents: list[AgentState], now: datetime
) -> dict[int, Observation]:
    """Advance the per-task observation only when the sequence actually moved."""
    seen = dict(prev.seen)
    out: dict[int, Observation] = {}
    for agent in agents:
        old = seen.get(agent.task)
        if old is None or old.change_seq != agent.change_seq:
            out[agent.task] = Observation(change_seq=agent.change_seq, since=now)
        else:
            out[agent.task] = old
    return out


def _reap(
    ledger: list[Task],
    policy: Policy,
    agents: list[AgentState],
    observations: dict[int, Observation],
    now: datetime,
) -> list[Intent]:
    by_task = {a.task: a for a in agents}
    intents: list[Intent] = []
    for task in ledger:
        if task.state != "claimed":
            continue
        agent = by_task.get(task.number)
        reason = _failure_reason(task, agent, policy, observations, now)
        if reason is None:
            if agent is not None and agent.status == "done" and task.packet:
                intents.append(Advance(task=task.number, to=task.packet["to"]))
            continue
        if task.attempts >= policy.max_attempts:
            # The cap is on attempts already spent, so a task at the cap
            # escalates instead of being released for another try.
            intents.append(Escalate(task=task.number, reason="attempts_exhausted"))
        else:
            intents.append(Release(task=task.number, reason=reason))
    return intents


def _failure_reason(
    task: Task,
    agent: AgentState | None,
    policy: Policy,
    observations: dict[int, Observation],
    now: datetime,
) -> str | None:
    if agent is None:
        return "no_agent"
    if agent.cost_usd > policy.per_task_usd:
        return "over_budget"
    if agent.status == "dirty":
        return "dirty_exit"
    if agent.status in _FINISHED:
        return None if task.packet else "no_packet"
    observation = observations.get(task.number)
    if observation is not None:
        idle = now - observation.since
        if idle >= timedelta(minutes=policy.stall_minutes):
            return "stalled"
    return None


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
        spent_usd=prev.spent_usd + sum(a.cost_usd for a in agents),
    )
    return intents, state
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_decide_reap.py -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
git add src/fleet/decide.py tests/test_decide_reap.py
git commit -m "$(cat <<'EOF'
feat(decide): the REAP phase

Stall detection reads a monotonic counter, not a clock: herdr's snapshot has no
timestamp under any key, only state_change_seq and revision. An observation
advances its `since` only when the sequence actually moves, so "unchanged for T
minutes" is derivable from two ticks and a `now` parameter.

A task already at the attempt cap escalates instead of being released. Releasing
first and checking later is the shape that produces a third attempt, which is
one of the four hard fails.

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
- Consumes: `decide` from Task 8
- Produces: `decide` additionally emits `Unblock(task)` intents.

- [ ] **Step 1: Write the failing test**

Create `tests/test_decide_unblock.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from fleet.decide import decide
from fleet.model import FleetState, Task, Unblock

from test_decide_reap import POLICY

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)


def blocked(number=4, on=11):
    return Task(
        number=number, title="t", state="blocked", area="models", klass="patch",
        auto_ok=True, blocked_on=on, packet={"to": "blocked", "blocked_on": on},
    )


def done(number=11):
    return Task(
        number=number, title="t", state="done", area="data", klass="patch", auto_ok=True
    )


def test_a_blocked_task_whose_dependency_is_done_is_requeued():
    """Harness §4 issue 4: requeued unattended, no human. One of the four hard
    fails."""
    intents, _ = decide(FleetState(), [blocked(), done()], POLICY, [], T0)
    assert Unblock(task=4) in intents


def test_a_blocked_task_whose_dependency_is_open_stays_blocked():
    waiting = Task(
        number=11, title="t", state="claimed", area="data", klass="patch", auto_ok=True
    )
    intents, _ = decide(FleetState(), [blocked(), waiting], POLICY, [], T0)
    assert Unblock(task=4) not in intents


def test_a_dependency_that_is_not_in_the_ledger_does_not_unblock():
    """A missing dependency is a reason to leave the task blocked, not to assume
    it finished. Assuming would requeue a task whose prerequisite never landed."""
    intents, _ = decide(FleetState(), [blocked(on=99)], POLICY, [], T0)
    assert Unblock(task=4) not in intents


def test_a_blocked_task_with_no_dependency_recorded_does_not_unblock():
    orphan = Task(
        number=4, title="t", state="blocked", area="models", klass="patch", auto_ok=True
    )
    intents, _ = decide(FleetState(), [orphan], POLICY, [], T0)
    assert Unblock(task=4) not in intents


def test_unblocking_wakes_no_human():
    """block and escalate are deliberately distinct. Collapsed into one state, a
    morning queue fills with tasks that could have resolved themselves at 3am."""
    from fleet.model import Escalate

    intents, _ = decide(FleetState(), [blocked(), done()], POLICY, [], T0)
    assert not [i for i in intents if isinstance(i, Escalate)]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_decide_unblock.py -v`
Expected: FAIL — `Unblock(task=4) not in intents`

- [ ] **Step 3: Implement**

In `src/fleet/decide.py`, add `Unblock` to the `fleet.model` import, then add:

```python
def _unblock(ledger: list[Task]) -> list[Intent]:
    """BLOCKED tasks whose dependency reached DONE. No human is involved -- that
    is the entire reason `block` is kept distinct from `escalate`."""
    done = {t.number for t in ledger if t.state == "done"}
    return [
        Unblock(task=t.number)
        for t in ledger
        # `blocked_on is None` stays blocked rather than unblocking: a task with
        # no recorded dependency is a bug to surface, not a task to requeue.
        if t.state == "blocked" and t.blocked_on is not None and t.blocked_on in done
    ]
```

and in `decide`, after the reap line:

```python
    intents += _unblock(ledger)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_decide_unblock.py -v`
Expected: PASS, 5 tests

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
- Consumes: `decide` from Task 9
- Produces: `decide` additionally emits `Dispatch(task, role, base)` and `Drain(reason)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_decide_dispatch.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from fleet.decide import decide
from fleet.model import AgentState, Dispatch, Drain, FleetState, Task

from test_decide_reap import POLICY

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)


def ready(number, area="eval", klass="patch", auto_ok=True):
    return Task(
        number=number, title="t", state="ready", area=area, klass=klass, auto_ok=auto_ok
    )


def dispatched(intents):
    return sorted(i.task for i in intents if isinstance(i, Dispatch))


def test_a_ready_auto_ok_task_is_dispatched_to_the_first_role_in_its_route():
    intents, _ = decide(FleetState(), [ready(1)], POLICY, [], T0)
    assert Dispatch(task=1, role="implementer", base="integration") in intents


def test_two_ready_tasks_in_one_area_yield_exactly_one_dispatch():
    """Parent §11's first case, and harness §4's issues 2 and 3."""
    intents, _ = decide(FleetState(), [ready(2, "train"), ready(3, "train")], POLICY, [], T0)
    assert len(dispatched(intents)) == 1


def test_two_ready_tasks_in_different_areas_both_dispatch():
    intents, _ = decide(FleetState(), [ready(1, "eval"), ready(2, "train")], POLICY, [], T0)
    assert dispatched(intents) == [1, 2]


def test_an_area_already_held_by_a_claimed_task_is_excluded():
    live = Task(
        number=1, title="t", state="claimed", area="eval", klass="patch", auto_ok=True
    )
    agents = [AgentState(task=1, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [live, ready(8, "eval")], POLICY, agents, T0)
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
    integrating = Task(
        number=20, title="t", state="claimed", area=None, klass="integration", auto_ok=True
    )
    agents = [AgentState(task=20, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [integrating, ready(1, "eval")], POLICY, agents, T0)
    assert dispatched(intents) == []


def test_slots_bound_the_number_of_dispatches():
    small = POLICY.__class__(**{**vars(POLICY), "slots": 2})
    backlog = [ready(1, "eval"), ready(2, "train"), ready(3, "models"), ready(4, "data")]
    intents, _ = decide(FleetState(), backlog, small, [], T0)
    assert len(dispatched(intents)) == 2


def test_live_agents_consume_slots():
    live = Task(
        number=1, title="t", state="claimed", area="eval", klass="patch", auto_ok=True
    )
    small = POLICY.__class__(**{**vars(POLICY), "slots": 1})
    agents = [AgentState(task=1, status="working", change_seq=1)]
    intents, _ = decide(FleetState(), [live, ready(2, "train")], small, agents, T0)
    assert dispatched(intents) == []


def test_at_eighty_percent_of_budget_the_fleet_drains_rather_than_killing():
    """Parent §10: drain, do not kill. In-flight tasks finish; nothing new
    starts. Killing at the ceiling throws away work already paid for."""
    spent = FleetState(spent_usd=20.0)  # 80% of 25.0
    intents, _ = decide(spent, [ready(1, "eval")], POLICY, [], T0)
    assert dispatched(intents) == []
    assert Drain(reason="budget") in intents


def test_below_the_drain_threshold_dispatch_continues():
    intents, _ = decide(FleetState(spent_usd=19.9), [ready(1, "eval")], POLICY, [], T0)
    assert dispatched(intents) == [1]


def test_draining_does_not_stop_reaping_or_unblocking():
    """Drain stops dispatch only. A stalled agent must still be released, or the
    fleet drains into a state nothing cleans up."""
    from fleet.model import Release

    live = Task(
        number=1, title="t", state="claimed", area="eval", klass="patch", auto_ok=True
    )
    intents, _ = decide(FleetState(spent_usd=25.0), [live], POLICY, [], T0)
    assert Release(task=1, reason="no_agent") in intents


def test_a_task_in_triage_is_never_dispatched():
    """Discoveries land in TRIAGE. An agent that could both file and claim work
    has authorized itself."""
    triage = Task(
        number=13, title="t", state="triage", area="data", klass=None, auto_ok=False
    )
    intents, _ = decide(FleetState(), [triage], POLICY, [], T0)
    assert dispatched(intents) == []


def test_a_class_with_no_route_is_not_dispatched():
    intents, _ = decide(FleetState(), [ready(9, "docs", klass="spec")], POLICY, [], T0)
    assert dispatched(intents) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_decide_dispatch.py -v`
Expected: FAIL — no `Dispatch` intents emitted

- [ ] **Step 3: Implement**

In `src/fleet/decide.py`, add `Dispatch` and `Drain` to the imports, then add:

```python
_DRAIN_FRACTION = 0.8
_LIVE = {"claimed", "review", "integrating"}


def _dispatch(
    ledger: list[Task], policy: Policy, agents: list[AgentState], spent_usd: float
) -> list[Intent]:
    if policy.fleet_usd > 0 and spent_usd >= policy.fleet_usd * _DRAIN_FRACTION:
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
        intents.append(Dispatch(task=task.number, role=route[0], base="integration"))
        if task.area is not None:
            held.add(task.area)
        free -= 1
    return intents
```

and in `decide`, after the unblock line:

```python
    intents += _dispatch(ledger, policy, agents, prev.spent_usd)
```

Note `_dispatch` reads `prev.spent_usd`, not the newly-computed total: the
threshold is what was already spent when the tick began.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_decide_dispatch.py -v`
Expected: PASS, 14 tests

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
- Consumes: intents and effects from Task 3; `labels` (existing)
- Produces: `expand(intent: Intent, policy: Policy) -> list[Effect]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_expand.py`:

```python
from __future__ import annotations

import pytest

from fleet.expand import expand
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
    every label query. One effect means one gh call means no window."""
    effects = expand(Unblock(task=4), POLICY)
    assert len(labels_of(effects)) == 1
    only = labels_of(effects)[0]
    assert "fleet:ready" in only.add
    assert "fleet:blocked" in only.remove


def test_dispatch_assigns_starts_and_labels():
    effects = expand(Dispatch(task=1, role="implementer", base="integration"), POLICY)
    assert StartAgent(task=1, role="implementer", base="integration") in effects
    assert any(isinstance(e, Assign) and e.task == 1 and e.owner for e in effects)
    add = labels_of(effects)[0].add
    assert "fleet:claimed" in add
    assert "fleet:attempt-1" in add


def test_release_stops_the_agent_unassigns_and_increments_the_attempt():
    effects = expand(Release(task=1, reason="stalled"), POLICY, attempts=1)
    assert StopAgent(task=1) in effects
    assert Assign(task=1, owner=None) in effects
    add = labels_of(effects)[0].add
    assert "fleet:ready" in add
    assert "fleet:attempt-2" in add


def test_release_never_emits_an_attempt_label_beyond_the_cap():
    """labels.static_vocabulary is sized from max_attempts, so a label past the
    cap is one gh has never heard of -- and gh refuses an unknown label and
    changes nothing at all."""
    effects = expand(Release(task=1, reason="stalled"), POLICY, attempts=2)
    add = labels_of(effects)[0].add
    assert "fleet:attempt-3" not in add


def test_escalate_reaches_human_and_stops_the_agent():
    effects = expand(Escalate(task=1, reason="attempts_exhausted"), POLICY)
    assert StopAgent(task=1) in effects
    assert "fleet:human" in labels_of(effects)[0].add


def test_advance_moves_to_the_named_state():
    effects = expand(Advance(task=1, to="review"), POLICY)
    assert "fleet:review" in labels_of(effects)[0].add


def test_advance_refuses_a_state_outside_the_vocabulary():
    """A packet's `to` reaches here from an issue comment, so it is not
    necessarily a state this repo's label set contains."""
    with pytest.raises(ValueError, match="unknown state"):
        expand(Advance(task=1, to="merged"), POLICY)


def test_drain_produces_no_effects():
    """Drain is a decision not to dispatch. There is nothing to write."""
    assert expand(Drain(reason="budget"), POLICY) == []


def test_every_removal_is_within_the_static_vocabulary():
    """gh resolves a label to an id before removing it, so removing a label the
    repo has never had fails the whole edit. FakeGh mirrors that."""
    from fleet.labels import static_vocabulary

    vocabulary = static_vocabulary(POLICY.max_attempts, POLICY.max_fix_passes)
    for intent in [
        Unblock(task=4),
        Dispatch(task=1, role="implementer", base="integration"),
        Release(task=1, reason="stalled"),
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
assert Dispatch(5, "implementer"), not a list of label strings -- while the
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
    """One effect carrying both halves, so the executor makes one gh call."""
    return SetLabels(
        task=task,
        add=(labels_module.state_label(state), *extra),
        remove=tuple(labels_module.all_state_labels()),
    )


def expand(intent: Intent, policy: Policy, *, attempts: int = 0) -> list[Effect]:
    if isinstance(intent, Drain):
        return []

    if isinstance(intent, Dispatch):
        return [
            _to_state(intent.task, "claimed", (labels_module.attempt_label(1),)),
            Assign(task=intent.task, owner=f"fleet-{intent.task}"),
            StartAgent(task=intent.task, role=intent.role, base=intent.base),
        ]

    if isinstance(intent, Release):
        # Capped at max_attempts: labels.static_vocabulary is sized from that
        # number, so attempt-(cap+1) is a label gh has never heard of, and gh
        # refuses an unknown label and changes nothing at all.
        nxt = min(attempts + 1, policy.max_attempts)
        return [
            StopAgent(task=intent.task),
            Assign(task=intent.task, owner=None),
            _to_state(intent.task, "ready", (labels_module.attempt_label(nxt),)),
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
        return [_to_state(intent.task, intent.to)]

    raise ValueError(f"cannot expand {intent!r}")
```

Note: `state_label` already raises `ValueError("unknown state: ...")`; the
explicit check in `Advance` exists so the message names the packet as the source.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_expand.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add src/fleet/expand.py tests/test_expand.py
git commit -m "$(cat <<'EOF'
feat(expand): intents to effects, purely

A state transition is exactly one SetLabels effect carrying both the additions
and the removals, so the executor makes one gh call. commands._post's
remove-all-eight-then-add-one has a window in which a crash leaves the task with
no state label, invisible to every label query.

The attempt label is capped at max_attempts. labels.static_vocabulary is sized
from that number, so attempt-(cap+1) is a label gh has never heard of -- and gh
refuses an unknown label by failing the whole edit and changing nothing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: `executor.py` — the seam and the fake

**Files:**
- Create: `src/fleet/executor.py`
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: effects and `AgentState` from Task 3
- Produces: `Executor` Protocol with `apply(effects: list[Effect]) -> None` and `agents() -> list[AgentState]`; `FakeExecutor(outcomes: dict[int, str] | None = None)` with `script(task, outcome)`, `tick()`, and `applied: list[Effect]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_executor.py`:

```python
from __future__ import annotations

import pytest

from fleet.executor import FakeExecutor
from fleet.model import Assign, SetLabels, StartAgent, StopAgent


def test_starting_an_agent_makes_it_visible_as_working():
    ex = FakeExecutor()
    ex.apply([StartAgent(task=1, role="implementer", base="integration")])
    assert [a.task for a in ex.agents()] == [1]
    assert ex.agents()[0].status == "working"


def test_a_working_agents_sequence_advances_each_tick():
    """Stall detection reads a moving sequence, so a healthy agent must move."""
    ex = FakeExecutor()
    ex.apply([StartAgent(task=1, role="implementer", base="integration")])
    first = ex.agents()[0].change_seq
    ex.tick()
    assert ex.agents()[0].change_seq > first


def test_a_stalled_agent_stops_advancing_but_stays_working():
    """The exact shape of the failure: nominally running, producing nothing."""
    ex = FakeExecutor()
    ex.apply([StartAgent(task=1, role="implementer", base="integration")])
    ex.script(1, "stall")
    ex.tick()
    seq = ex.agents()[0].change_seq
    ex.tick()
    assert ex.agents()[0].change_seq == seq
    assert ex.agents()[0].status == "working"


def test_a_clean_exit_reports_done():
    ex = FakeExecutor()
    ex.apply([StartAgent(task=1, role="implementer", base="integration")])
    ex.script(1, "clean_exit")
    ex.tick()
    assert ex.agents()[0].status == "done"


def test_a_dirty_exit_reports_dirty():
    ex = FakeExecutor()
    ex.apply([StartAgent(task=1, role="implementer", base="integration")])
    ex.script(1, "dirty_exit")
    ex.tick()
    assert ex.agents()[0].status == "dirty"


def test_over_budget_accrues_cost_past_the_per_task_ceiling():
    ex = FakeExecutor()
    ex.apply([StartAgent(task=1, role="implementer", base="integration")])
    ex.script(1, "over_budget")
    ex.tick()
    assert ex.agents()[0].cost_usd > 2.0


def test_stopping_an_agent_removes_it():
    ex = FakeExecutor()
    ex.apply([StartAgent(task=1, role="implementer", base="integration")])
    ex.apply([StopAgent(task=1)])
    assert ex.agents() == []


def test_applied_effects_are_recorded_in_order():
    """Scenarios assert on what was written, so the record is the interface."""
    ex = FakeExecutor()
    effects = [SetLabels(task=1, add=("fleet:claimed",)), Assign(task=1, owner="fleet-1")]
    ex.apply(effects)
    assert ex.applied == effects


def test_an_unknown_outcome_is_refused():
    """A typo in a scenario file would otherwise silently script a healthy agent
    and the scenario would pass while proving nothing."""
    ex = FakeExecutor()
    with pytest.raises(ValueError, match="unknown outcome"):
        ex.script(1, "explodes")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_executor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.executor'`

- [ ] **Step 3: Implement**

Create `src/fleet/executor.py`:

```python
"""The executor seam, with the fake behind it.

Building the seam now is nearly free because decide() is already pure.
Retrofitting it after a daemon exists is not -- and the four failure modes that
matter most overnight (stall, poison task, conflict storm, budget drain) are the
four hardest to reproduce with real agents. Scripting them is the only way they
become tests.
"""

from __future__ import annotations

from typing import Protocol

from fleet.model import AgentState, Effect, StartAgent, StopAgent

OUTCOMES = ("healthy", "clean_exit", "dirty_exit", "stall", "over_budget", "conflict")

_STATUS = {
    "healthy": "working",
    "stall": "working",
    "over_budget": "working",
    "conflict": "dirty",
    "clean_exit": "done",
    "dirty_exit": "dirty",
}

_COST_PER_TICK = {"over_budget": 3.0}


class Executor(Protocol):
    def apply(self, effects: list[Effect]) -> None: ...
    def agents(self) -> list[AgentState]: ...


class FakeExecutor:
    """Scripted outcomes, no I/O. The only executor M2 has."""

    def __init__(self, outcomes: dict[int, str] | None = None) -> None:
        self.applied: list[Effect] = []
        self._outcomes: dict[int, str] = dict(outcomes or {})
        self._live: dict[int, AgentState] = {}
        self._seq = 0

    def script(self, task: int, outcome: str) -> None:
        if outcome not in OUTCOMES:
            # A typo would otherwise script a healthy agent, and the scenario
            # would pass while proving nothing.
            raise ValueError(f"unknown outcome {outcome!r}; expected one of {OUTCOMES}")
        self._outcomes[task] = outcome

    def apply(self, effects: list[Effect]) -> None:
        self.applied.extend(effects)
        for effect in effects:
            if isinstance(effect, StartAgent):
                self._seq += 1
                self._live[effect.task] = AgentState(
                    task=effect.task, status="working", change_seq=self._seq, cost_usd=0.0
                )
            elif isinstance(effect, StopAgent):
                self._live.pop(effect.task, None)

    def tick(self) -> None:
        """Advance every live agent by its scripted outcome."""
        for task, agent in list(self._live.items()):
            outcome = self._outcomes.get(task, "healthy")
            self._seq += 1
            # A stalled agent is nominally running and producing nothing, so its
            # sequence must NOT move -- that is the only signal there is.
            seq = agent.change_seq if outcome == "stall" else self._seq
            self._live[task] = AgentState(
                task=task,
                status=_STATUS[outcome],
                change_seq=seq,
                cost_usd=agent.cost_usd + _COST_PER_TICK.get(outcome, 0.1),
            )

    def agents(self) -> list[AgentState]:
        return sorted(self._live.values(), key=lambda a: a.task)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_executor.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add src/fleet/executor.py tests/test_executor.py
git commit -m "$(cat <<'EOF'
feat(executor): the seam and a scripted fake

A stalled agent keeps its change_seq and stays "working". That is the whole
shape of the failure -- nominally running, producing nothing -- and the sequence
not moving is the only signal there is, because herdr's snapshot carries no
timestamp.

An unknown outcome raises. A typo in a scenario file would otherwise script a
healthy agent, and the scenario would pass while proving nothing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: `runlog.py`

**Files:**
- Create: `src/fleet/runlog.py`
- Test: `tests/test_runlog.py`

**Interfaces:**
- Consumes: intents from Task 3
- Produces: `RunLog(path: Path, run_id: str)` with `append(tick: int, at: datetime, intent: Intent, outcome: str) -> None` and `read(path) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_runlog.py`:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone

from fleet.model import Dispatch, Release, Unblock
from fleet.runlog import RunLog, read

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)


def test_each_entry_is_one_json_line(tmp_path):
    log = RunLog(tmp_path / "run.jsonl", run_id="r1")
    log.append(tick=1, at=T0, intent=Dispatch(task=1, role="implementer", base="integration"),
               outcome="applied")
    log.append(tick=2, at=T0, intent=Release(task=1, reason="stalled"), outcome="applied")
    lines = (tmp_path / "run.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["intent"]["kind"] == "Dispatch"


def test_the_intents_fields_are_recorded_not_just_its_name(tmp_path):
    log = RunLog(tmp_path / "run.jsonl", run_id="r1")
    log.append(tick=1, at=T0, intent=Release(task=4, reason="stalled"), outcome="applied")
    entry = read(tmp_path / "run.jsonl")[0]
    assert entry["intent"] == {"kind": "Release", "task": 4, "reason": "stalled"}


def test_a_transient_label_survives_in_the_log(tmp_path):
    """m2-inputs §3: rubric 04 asserts fleet:blocked was applied, but the next
    transition removes it, so the final ledger cannot answer "was it ever
    applied". Only this file can."""
    log = RunLog(tmp_path / "run.jsonl", run_id="r1")
    log.append(tick=1, at=T0, intent=Unblock(task=4), outcome="applied")
    assert any(e["intent"]["kind"] == "Unblock" for e in read(tmp_path / "run.jsonl"))


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
final ledger and `git log integration`. Nothing reads it in M2; writing it later
would mean reconstructing a night that has already happened.

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
            handle.write(json.dumps(entry, sort_keys=False) + "\n")


def read(path: Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines() if line]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_runlog.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add src/fleet/runlog.py tests/test_runlog.py
git commit -m "$(cat <<'EOF'
feat(runlog): append-only JSONL of every intent

Harness §7 makes this one of the three inputs `fleet grade` needs. Nothing reads
it in M2, but writing it later would mean reconstructing a night that has
already happened.

It is also the only place a transient label survives: rubric 04 asserts
fleet:blocked was applied, and the next transition removes it, so the final
ledger cannot answer the question.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 14: `simulate.py` — the scenario runner

**Files:**
- Create: `src/fleet/simulate.py`
- Test: `tests/test_simulate.py`

**Interfaces:**
- Consumes: `decide`, `expand`, `FakeExecutor`, `ledger.read`, `policy.load`
- Produces: `run(scenario: dict, root: Path) -> list[list[Intent]]` (one entry per tick) and `load_scenario(path: Path) -> dict`, plus `ScenarioError`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_simulate.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import FIXTURE_POLICY
from fleet.model import Dispatch, Escalate, Release
from fleet.simulate import ScenarioError, load_scenario, run

ROOT = Path(__file__).resolve().parents[1]

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
    "ticks": [{"outcomes": {}, "expect": ["Dispatch(1, implementer)"]}],
}


def test_an_inline_scenario_dispatches():
    ticks = run(INLINE, ROOT)
    assert Dispatch(task=1, role="implementer", base="integration") in ticks[0]


def test_expectations_are_checked_not_merely_recorded():
    """A harness that records without asserting is a harness that always passes."""
    wrong = {**INLINE, "ticks": [{"outcomes": {}, "expect": ["Dispatch(99, implementer)"]}]}
    with pytest.raises(AssertionError, match="tick 1"):
        run(wrong, ROOT)


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


def test_a_scenario_can_seed_from_the_captured_ledger():
    scenario = {
        "name": "captured",
        "proves": "real shapes",
        "seed": {"ledger": "fixtures/ledger/issues.json", "policy": str(FIXTURE_POLICY)},
        "ticks": [{"outcomes": {}, "expect": None}],
    }
    ticks = run(scenario, ROOT)
    assert any(isinstance(i, Dispatch) for i in ticks[0])


def test_time_advances_by_one_stall_threshold_per_tick():
    """A stall takes stall_minutes of simulated time and no wall-clock at all."""
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer)"]},
            {"outcomes": {1: "stall"}, "expect": None},
            {"outcomes": {1: "stall"}, "expect": ["Release(1, stalled)"]},
        ],
    }
    ticks = run(scenario, ROOT)
    assert Release(task=1, reason="stalled") in ticks[2]


def test_an_unknown_scenario_key_is_refused():
    with pytest.raises(ScenarioError, match="unknown key"):
        run({**INLINE, "tickz": []}, ROOT)


def test_a_scenario_without_ticks_is_refused():
    with pytest.raises(ScenarioError, match="no ticks"):
        run({"name": "x", "proves": "y", "seed": {"issues": [], "policy": "."}}, ROOT)


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
from fleet.model import FleetState, Intent

START = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)

_KEYS = {"name", "proves", "seed", "ticks"}
_SEED_KEYS = {"issues", "ledger", "policy"}
_CALL = re.compile(r"^(\w+)\((.*)\)$")


class ScenarioError(Exception):
    """A scenario file could not be understood."""


def load_scenario(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def _check(scenario: dict) -> None:
    unknown = set(scenario) - _KEYS
    if unknown:
        raise ScenarioError(f"unknown key(s) in scenario: {sorted(unknown)}")
    if not scenario.get("ticks"):
        raise ScenarioError("scenario has no ticks")
    unknown_seed = set(scenario.get("seed", {})) - _SEED_KEYS
    if unknown_seed:
        raise ScenarioError(f"unknown key(s) in seed: {sorted(unknown_seed)}")


def _render(intent: Intent) -> str:
    """`Dispatch(1, implementer)` — the form scenarios are written in."""
    values = [str(v) for k, v in vars(intent).items() if k != "base"]
    return f"{type(intent).__name__}({', '.join(values)})"


def _rows(seed: dict, root: Path) -> list[dict]:
    if "ledger" in seed:
        return json.loads((root / seed["ledger"]).read_text())
    return list(seed.get("issues", []))


def run(scenario: dict, root: Path) -> list[list[Intent]]:
    _check(scenario)
    seed = scenario["seed"]
    cfg = load_config(Path(seed["policy"]))
    policy = policy_module.load(cfg)
    rows = _rows(seed, root)

    executor = FakeExecutor()
    state = FleetState()
    now = START
    history: list[list[Intent]] = []

    for index, tick in enumerate(scenario["ticks"], start=1):
        for task, outcome in (tick.get("outcomes") or {}).items():
            executor.script(int(task), outcome)
        executor.tick()

        tasks = ledger_module.read(rows)
        intents, state = decide(state, tasks, policy, executor.agents(), now)
        history.append(intents)

        for intent in intents:
            executor.apply(expand(intent, policy))
            rows = _apply_to_rows(rows, intent, policy)

        expected = tick.get("expect")
        if expected is not None:
            # `expect: []` asserts an absence, which is the shape of the poison
            # task claim -- "never a third attempt". Treating an empty list as
            # "do not check" would leave that claim unproven.
            actual = sorted(_render(i) for i in intents)
            if actual != sorted(expected):
                raise AssertionError(
                    f"tick {index} of {scenario['name']}: expected {sorted(expected)}, got {actual}"
                )
        now += timedelta(minutes=policy.stall_minutes)

    return history


def _apply_to_rows(rows: list[dict], intent: Intent, policy) -> list[dict]:
    """Fold the intent's label effects back into the seeded rows, so the next
    tick reads a ledger that reflects the last one."""
    updated = [dict(row, labels=list(row["labels"])) for row in rows]
    by_number = {row["number"]: row for row in updated}
    for effect in expand(intent, policy, attempts=_attempts(by_number, intent)):
        target = by_number.get(getattr(effect, "task", None))
        if target is None or not hasattr(effect, "add"):
            continue
        names = [
            n
            for n in ledger_module._names(target["labels"])
            if n not in getattr(effect, "remove", ())
        ]
        target["labels"] = names + [n for n in effect.add if n not in names]
    return updated


def _attempts(by_number: dict, intent: Intent) -> int:
    row = by_number.get(getattr(intent, "task", None))
    if row is None:
        return 0
    return ledger_module._counter(ledger_module._names(row["labels"]), "fleet:attempt-")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_simulate.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add src/fleet/simulate.py tests/test_simulate.py
git commit -m "$(cat <<'EOF'
feat(simulate): replay a night through decide(), offline

`expect: []` asserts an absence rather than skipping the check. The poison-task
scenario's final tick is exactly that shape -- "never a third attempt" -- and a
harness treating an empty list as "do not check" would leave one of the four
hard fails unproven while reporting a pass.

Time advances one stall threshold per tick, so a stall costs simulated minutes
and no wall-clock.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: The scenario set

One file per row of parent §10's failure table and per case in §11.

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


def test_there_are_scenarios_at_all():
    """A glob that matches nothing makes the parametrized test below vacuous --
    it would report a pass having run zero scenarios."""
    assert len(SCENARIOS) >= 7


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenario(path):
    run(load_scenario(path), ROOT)


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_every_scenario_says_what_it_proves(path):
    scenario = load_scenario(path)
    assert scenario["proves"].strip()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_scenarios.py -v`
Expected: FAIL — `assert 0 >= 7`

- [ ] **Step 3: Write the scenarios**

Each uses `policy: fixtures/policy/fleet-fixture`. Create all seven:

`fixtures/scenarios/area-exclusion.yaml`:
```yaml
name: area-exclusion
proves: "parent §11 'two READY tasks in one area → exactly one dispatched'; harness §4 issues 2 and 3"
seed:
  policy: fixtures/policy/fleet-fixture
  issues:
    - {number: 2, title: "train history", state: OPEN, assignees: [],
       labels: ["fleet:ready", "fleet:auto-ok", "area:train", "class:patch"]}
    - {number: 3, title: "train best step", state: OPEN, assignees: [],
       labels: ["fleet:ready", "fleet:auto-ok", "area:train", "class:patch"]}
ticks:
  - outcomes: {}
    expect: ["Dispatch(2, implementer)"]
  - outcomes: {}
    expect: []
```

`fixtures/scenarios/poison-task.yaml`:
```yaml
name: poison-task
proves: "parent §10 poison task; harness §7 hard fail 'no task exceeds 2 attempts'"
seed:
  policy: fixtures/policy/fleet-fixture
  issues:
    - {number: 5, title: "deterministic shuffle", state: OPEN, assignees: [],
       labels: ["fleet:ready", "fleet:auto-ok", "area:data", "class:patch"]}
ticks:
  - outcomes: {}
    expect: ["Dispatch(5, implementer)"]
  - outcomes: {5: dirty_exit}
    expect: ["Release(5, dirty_exit)"]
  - outcomes: {}
    expect: ["Dispatch(5, implementer)"]
  - outcomes: {5: dirty_exit}
    expect: ["Escalate(5, attempts_exhausted)"]
  - outcomes: {}
    expect: []
```

`fixtures/scenarios/stall-releases.yaml`:
```yaml
name: stall-releases
proves: "parent §10 silent stall: output unchanged for T minutes → release, attempt += 1"
seed:
  policy: fixtures/policy/fleet-fixture
  issues:
    - {number: 1, title: "lid small sample", state: OPEN, assignees: [],
       labels: ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"]}
ticks:
  - outcomes: {}
    expect: ["Dispatch(1, implementer)"]
  - outcomes: {1: stall}
    expect: []
  - outcomes: {1: stall}
    expect: ["Release(1, stalled)"]
```

`fixtures/scenarios/blocked-self-resolves.yaml`:
```yaml
name: blocked-self-resolves
proves: "parent §11 'blocked task's dependency closes → requeued with no human'; harness §7 hard fail, issue 4"
seed:
  policy: fixtures/policy/fleet-fixture
  issues:
    - {number: 4, title: "generator needs reference", state: OPEN, assignees: [],
       labels: ["fleet:blocked", "fleet:auto-ok", "area:models", "class:patch"]}
    - {number: 11, title: "make_reference", state: OPEN, assignees: [],
       labels: ["fleet:done", "fleet:auto-ok", "area:data", "class:patch"]}
ticks:
  - outcomes: {}
    expect: ["Unblock(4)"]
  - outcomes: {}
    expect: ["Dispatch(4, implementer)"]
```

Note: this scenario needs `blocked_on` on task 4, which `ledger.read` takes from
the packet. Add a `packets` key to the seed in Task 14 if the scenario cannot
express it — otherwise set the dependency through the issue's packet fixture.
**If `run()` cannot supply a packet, extend `_rows` to accept
`seed.packets: {4: {to: blocked, blocked_on: 11}}` and pass it to
`ledger_module.read(rows, packets=...)`, adding `packets` to `_SEED_KEYS`.**

`fixtures/scenarios/budget-drains.yaml`:
```yaml
name: budget-drains
proves: "parent §10 runaway cost: at 80% drain, do not kill"
seed:
  policy: fixtures/policy/fleet-fixture
  issues:
    - {number: 1, title: "lid", state: OPEN, assignees: [],
       labels: ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"]}
    - {number: 2, title: "train", state: OPEN, assignees: [],
       labels: ["fleet:ready", "fleet:auto-ok", "area:train", "class:patch"]}
ticks:
  - outcomes: {}
    expect: ["Dispatch(1, implementer)", "Dispatch(2, implementer)"]
  - outcomes: {1: over_budget, 2: over_budget}
    expect: ["Release(1, over_budget)", "Release(2, over_budget)"]
```

`fixtures/scenarios/ledger-drift.yaml`:
```yaml
name: ledger-drift
proves: "parent §10 ledger drift: CLAIMED with no live agent → released; herdr state is the tiebreaker"
seed:
  policy: fixtures/policy/fleet-fixture
  issues:
    - {number: 1, title: "lid", state: OPEN, assignees: [],
       labels: ["fleet:claimed", "fleet:auto-ok", "fleet:attempt-1", "area:eval", "class:patch"]}
ticks:
  - outcomes: {}
    expect: ["Release(1, no_agent)"]
```

`fixtures/scenarios/integration-lock.yaml`:
```yaml
name: integration-lock
proves: "harness amendment 2: while a class:integration task is live, the allocator dispatches nothing new"
seed:
  policy: fixtures/policy/fleet-fixture
  issues:
    - {number: 20, title: "integrate epic 9", state: OPEN, assignees: [],
       labels: ["fleet:claimed", "fleet:auto-ok", "class:integration"]}
    - {number: 1, title: "lid", state: OPEN, assignees: [],
       labels: ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"]}
ticks:
  - outcomes: {}
    expect: ["Release(20, no_agent)"]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_scenarios.py -v`
Expected: PASS, 15 tests (7 scenarios × 2 + the count guard)

If `blocked-self-resolves` fails for want of a packet, apply the `seed.packets`
extension described above, then re-run.

- [ ] **Step 5: Commit**

```bash
git add fixtures/scenarios tests/test_scenarios.py src/fleet/simulate.py
git commit -m "$(cat <<'EOF'
test(scenarios): one file per failure-table row

Parent §10's stall, poison task, runaway cost and ledger drift, plus §11's area
exclusion and auto-unblock, plus harness amendment 2's integration lock.

The count guard is not ceremony: a glob that matches nothing makes a
parametrized test vacuous, so the suite would report a pass having run zero
scenarios.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 16: `fleet plan --dry-run`

**Files:**
- Modify: `src/fleet/cli.py`
- Test: `tests/test_plan.py`

**Interfaces:**
- Consumes: `decide`, `policy.load`, `ledger.read`, `RealGh`
- Produces: `fleet plan [--dry-run/--no-dry-run]`, and `cli.ReadOnlyGh` — a wrapper that raises `PermissionError` on every mutating method.

- [ ] **Step 1: Write the failing test**

Create `tests/test_plan.py`:

```python
from __future__ import annotations

import pytest

from fleet.cli import ReadOnlyGh
from fleet.gh import FakeGh

WRITES = [
    ("create_issue", ("t", "b", [])),
    ("comment", (1, "hi")),
    ("add_labels", (1, ["fleet:ready"])),
    ("remove_labels", (1, ["fleet:ready"])),
    ("ensure_label", ("x",)),
]


@pytest.fixture()
def gh():
    client = FakeGh()
    client.create_issue("t", "b", ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"])
    return client


@pytest.mark.parametrize("method,args", WRITES, ids=[m for m, _ in WRITES])
def test_every_mutating_method_is_refused(gh, method, args):
    """"Writes nothing" is an assertion about the code path, not about having
    read it carefully. Same reasoning that made FakeGh refuse unknown labels: a
    stand-in more permissive than the real thing is how M1's defects survived."""
    guarded = ReadOnlyGh(gh)
    with pytest.raises(PermissionError, match="dry-run"):
        getattr(guarded, method)(*args)


def test_reads_still_work(gh):
    guarded = ReadOnlyGh(gh)
    assert guarded.list_issues("all")[0]["number"] == 1
    assert guarded.get_issue(1)["title"] == "t"


def test_the_ledger_is_unchanged_after_a_refused_write(gh):
    guarded = ReadOnlyGh(gh)
    with pytest.raises(PermissionError):
        guarded.comment(1, "hi")
    assert gh.get_issue(1)["comments"] == []


def test_read_only_covers_every_method_on_the_protocol():
    """A method added to GhClient later must be classified deliberately, not
    fall through to the underlying client and write."""
    from fleet.gh import GhClient

    surface = {n for n in dir(GhClient) if not n.startswith("_")}
    covered = ReadOnlyGh.READS | ReadOnlyGh.WRITES
    assert surface <= covered
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_plan.py -v`
Expected: FAIL — `ImportError: cannot import name 'ReadOnlyGh'`

- [ ] **Step 3: Implement**

In `src/fleet/cli.py`, add near the top:

```python
class ReadOnlyGh:
    """Wraps a GhClient and refuses every write.

    `--dry-run writes nothing` is a claim worth making mechanically. FakeGh was
    made to refuse unknown labels for the same reason: a stand-in more permissive
    than the real thing is how M1's defects survived the entire offline suite.
    """

    READS = frozenset({"get_issue", "list_issues", "search_titles"})
    WRITES = frozenset({"create_issue", "comment", "add_labels", "remove_labels", "ensure_label"})

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        if name in self.WRITES:
            def refuse(*_args, **_kwargs):
                raise PermissionError(f"dry-run: refusing to call {name}")

            return refuse
        if name in self.READS:
            return getattr(self._inner, name)
        raise AttributeError(f"{name} is neither a known read nor a known write")
```

and add the command:

```python
@main.command()
@click.option("--dry-run/--no-dry-run", default=True, help="Print the plan; write nothing.")
def plan(dry_run):
    """Print what the allocator would do right now."""
    from fleet import ledger as ledger_module
    from fleet import policy as policy_module
    from fleet.decide import decide
    from fleet.model import FleetState

    cfg = load_config()
    if not dry_run:
        raise click.ClickException("only --dry-run is implemented; the daemon lands in M3")
    gh = ReadOnlyGh(RealGh(cfg.repo_slug))
    tasks = ledger_module.read(gh.list_issues("open"))
    intents, _ = decide(
        FleetState(), tasks, policy_module.load(cfg), [], datetime.now(timezone.utc)
    )
    if not intents:
        click.echo("no action")
        return
    for intent in intents:
        click.echo(f"{type(intent).__name__}: {intent}")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_plan.py -v && uv run pytest`
Expected: PASS, full suite green

- [ ] **Step 5: Verify against the live ledger by hand**

```bash
cd ~/TIG/fleet-fixture && uv run --project ~/TIG/fleet fleet plan --dry-run
```
Expected: a list of `Dispatch:` lines for the ready backlog. Then confirm nothing moved:
```bash
gh issue list --repo FibonAdithya/fleet-fixture --state all --limit 200 \
  --json number,labels --jq '.[] | "\(.number) \([.labels[].name] | join(","))"'
```
Expected: identical to before the command ran.

- [ ] **Step 6: Commit**

```bash
git add src/fleet/cli.py tests/test_plan.py
git commit -m "$(cat <<'EOF'
feat(cli): fleet plan --dry-run

Reads the live ledger, runs one tick, prints the intents, writes nothing.

"Writes nothing" is enforced by a wrapper that refuses every mutating method,
and a test asserts the wrapper covers the whole GhClient surface -- so a method
added later must be classified deliberately rather than falling through to the
underlying client and writing.

--no-dry-run is refused rather than absent: the flag exists so the daemon in M3
has an obvious place to land, and an unimplemented write path should say so
instead of silently doing nothing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 17: Update the runbook and the M2 inputs ledger

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/m2-inputs.md`

**Interfaces:**
- Consumes: everything
- Produces: documentation only

- [ ] **Step 1: Add the new commands to the runbook**

In `docs/runbook.md`, under "The four write commands", change the heading to "The five write commands" and add:

```
    fleet verdict  --task N --verdict approved --head-sha SHA --checks-rerun "make check: pass"
```

and add a new section before "Known warts":

```markdown
## Seeing what the allocator would do

    cd ~/TIG/fleet-fixture && fleet plan --dry-run

Reads the ledger, runs one tick of `decide()`, prints the intents and writes
nothing. The client is wrapped so that every mutating method raises; the
guarantee is a property of the code path, not of having read it carefully.

`--no-dry-run` is refused until the daemon lands in M3.

## Running the scenarios

    uv run pytest tests/test_scenarios.py -v

Each file in `fixtures/scenarios/` proves one row of the design's failure table
and names it in its `proves` field.
```

Also update the "Agent environment" section to note that `FLEET_ROLE` is now
load-bearing:

```markdown
`FLEET_ROLE` is no longer only attribution: a handoff's target state is checked
against it. An implementer may hand to `review` and nothing else; only an
integrator may reach `done`. An unrecognised role is refused rather than
permitted, so a typo is an error and not a privilege escalation.
```

- [ ] **Step 2: Strike the resolved items from `docs/m2-inputs.md`**

Mark these as resolved, with the task that resolved them — do not delete the
text, since the file is a record of what executing the milestone found:

- §1 "Add `fleet verdict`" → **resolved, M2 Task 6**
- §2 "The allocator must gate on paths, not areas" → **resolved, M2 Task 10** — `decide()` gates on the label alone; path enforcement is the scout's and the integrator's
- §5 "Nothing binds a handoff's target state to the caller's role" → **resolved, M2 Task 7**
- §5 "The fleet suite depends on a hardcoded absolute path" → **resolved, M2 Task 1**
- §5 "`_post` can leave an issue with no state label" → **partially resolved, M2 Task 11** — `expand()` emits one combined effect; `commands._post` itself is unchanged

Add a line at the top of the file:

```markdown
> Items resolved by M2 are marked inline. The rest carry forward to M3.
```

- [ ] **Step 3: Run the full check**

Run: `make check`
Expected: all green

- [ ] **Step 4: Commit**

```bash
git add docs/runbook.md docs/m2-inputs.md
git commit -m "$(cat <<'EOF'
docs: record what M2 resolved and how to drive it

Marks resolved items inline in m2-inputs rather than deleting them -- the file
is a record of what executing the milestone found, and a struck item is more
useful than an absent one.

Notes in the runbook that FLEET_ROLE is now load-bearing rather than
attribution: a handoff's target state is checked against it.

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

- Spec §5 mentions `CreateIntegrationTask` (epic fan-in) as an intent. The type is defined in Task 3 but no task emits it, and no scenario covers parent §11's "last epic child closes → integration task auto-created". **This is a known, deliberate gap: fan-in needs `parent_epic` populated on `Task`, and nothing writes that label today** — `discover` cannot file a child with a class or a parent link (`m2-inputs` §5). Carrying it forward rather than faking it. Recorded in Task 17's doc update as an M3 item.
- Task 15's `blocked-self-resolves` scenario needs `blocked_on`, which comes from a packet rather than a label. Task 14's `_SEED_KEYS` therefore may need a `packets` key; the extension is spelled out inline in Task 15 Step 3 rather than left to be discovered.
- `ledger._names` and `ledger._counter` are private but used by `simulate._apply_to_rows`. Acceptable within one package; if a reviewer objects, promote both to public names in Task 14 rather than duplicating them.

**Type consistency:** `decide()`'s signature is fixed in Task 8 and unchanged by Tasks 9 and 10. `expand(intent, policy, *, attempts=0)` is introduced in Task 11 and called with `attempts=` only from `simulate._apply_to_rows`. `Task.klass` (not `class_`) is used consistently from Task 3 onward. `FakeExecutor.script/tick/apply/agents` match between Tasks 12, 14 and 15.

---

## Known scope boundaries

Carried to M3+, unchanged from `docs/m2-inputs.md`:

- Epic fan-out / fan-in (`CreateIntegrationTask` is defined and unused)
- The fixture's single-use seeder; a graded re-run is not yet possible
- The grader's 14 manual assertions and 8 human-needed rubrics
- Issue 5's escapable poison and issue 1/8's non-conflicting conflict pair
- `discover` cannot file a child with a class or acceptance criteria
- No shared `FleetError` base; rejected commands still print a traceback
- `close_issue` exists on `FakeGh` only and is absent from the `GhClient` Protocol
