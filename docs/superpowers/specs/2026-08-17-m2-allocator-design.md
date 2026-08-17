# M2: the allocator's pure core and its simulator

status: proposed
date: 2026-08-17
parent designs: `2026-08-10-agent-fleet-design.md`, `2026-08-10-fixture-harness-design.md`
inputs: `docs/m2-inputs.md`

## 1. What this is

M0 built the fixture and its policy layer. M1 built the CLI that agents write
through. M1.5 proved a reviewer can resume from a handoff packet with no
conversation history. None of it decides anything: there is still no code that
looks at a ledger and says which task should run next.

M2 is that code, and nothing that runs an agent. The deliverable is
`decide()` — a pure function from ledger, policy and agent states to a list of
intents — plus a fake executor that scripts agent outcomes, a scenario harness
that replays whole nights deterministically, and `fleet plan --dry-run` to point
the whole thing at the live ledger without writing to it.

### Why the pure core comes before the daemon

The parent design's §11 makes the claim this milestone rests on: the interesting
logic is testable without running an agent, because the allocator is a pure
function and the imperative shell around it is thin. M2 is where that claim gets
cashed. If `decide()` needs the network to be tested, the claim was wrong and
M3's daemon inherits the problem.

The harness design's §6 adds the second half: the four failure modes that matter
most overnight — a stall, a poison task, a conflict storm, a budget drain — are
the four hardest to reproduce with real agents. Waiting to observe them in the
wild is not a test strategy. A scripted executor makes each one a unit test.

### Goal

`make check` green, with a passing scenario for every row of parent §10's
failure table and every case in parent §11, and `fleet plan --dry-run` producing
a sensible action plan against the real `fleet-fixture` ledger while provably
writing nothing.

### Non-goals

- **No daemon, no state socket, no `fleet status`.** M3.
- **No `HerdrExecutor`.** The seam exists; only the fake implements it.
- **No integrator, no integrator guards.** M3 and M4 respectively.
- **No grader execution.** The run log lands; nothing reads it yet.
- **No fixture reproducibility work.** `docs/m2-inputs.md` §4 stays open.
- **No rubric rework, and issue 5's escapable poison stays escapable.** Both are
  fixture-validity problems, not allocator problems, and folding them in here
  would make M2 a milestone about two different things.

## 2. Scope, and the three pieces of M1 debt that come with it

Spec scope is `decide()`, `FakeExecutor`, the simulation harness and
`fleet plan --dry-run`. Three items from `docs/m2-inputs.md` are pulled in
because the state machine cannot run without them, and nothing else is.

| Item | Why it blocks M2 |
|---|---|
| `fleet verdict` | REVIEW has no legal exit. `verdict.schema.json` exists and nothing writes it, so a task that reaches REVIEW can never leave it. The patch route is implementer → reviewer → integrator; without a verdict, `decide()` is modelling a transition no agent can perform. |
| Bind handoff target state to `FLEET_ROLE` | An implementer can hand its own task to `done`, skipping review and integration. Grading reads terminal state from labels, so a self-declared `done` scores as correct. This is a grading integrity hole, and M2 is the milestone that starts producing gradeable state. |
| Parse `[budget]` in `config.py` | `fleet.toml` already carries `per_task_usd` and `fleet_usd`; `load_config` reads `[repo]`, `[policy]` and `[fleet]` only. Parent §10's "at 80%: drain, do not kill" is a `decide()` rule with no input wired. |

A fourth item comes in as a by-product rather than a blocker: vendoring the
fixture repo's policy layer and schemas into `fixtures/policy/`, which retires
`conftest.py`'s absolute path into another repo's working tree. Scenarios need a
policy seed regardless, so the fixture has to exist; pointing the existing
schema fixture at it is then a one-line change. See §7.

Everything else in `m2-inputs.md` — the fixture's single-use seeder, the grader's
14 manual assertions, the conflict pair that does not conflict, `discover`'s
inability to file what `planner.md` requires — is carried forward unchanged.

## 3. Architecture

```
src/fleet/
  model.py     Task · AgentState · Policy · FleetState · Intent · Effect
  ledger.py    raw gh JSON            → [Task]
  policy.py    fleet.toml + ownership.md + task-classes.md → Policy
  decide.py    decide(prev, ledger, policy, agents, now) → ([Intent], FleetState)
  expand.py    expand(intent, policy) → [Effect]
  executor.py  Executor Protocol · FakeExecutor
  runlog.py    JSONL append
  simulate.py  YAML scenario → ticks → assertions
```

Data flow, with every impure edge on the left:

```
gh issue list ──┐
gh issue view ──┼─→ ledger.read()   ─→ [Task]
herdr snapshot ─┼─→ shell normalize ─→ [AgentState]   task ← worktree path
fleet.toml ─────┴─→ policy.load()   ─→ Policy         idle ← prev + now
                                          │
              prev FleetState ────────────┤
                                          ▼
                          decide(…) → ([Intent], next FleetState)
                                          │
                       ┌──────────────────┼────────────────┐
                  expand(intent)     runlog.append    next FleetState
                       │                                (carried to next tick)
                   [Effect] ──→ Executor (Fake │ Herdr)
```

### `ledger.py` is the parity choke point

One place turns gh JSON into `Task`, so it is one place to test against both
`FakeGh` output and the captured `fixtures/ledger/issues.json`.

`docs/m2-inputs.md` §6 records that four of M0–M1's defects were fake/real
divergences, each of which passed the entire offline suite and would have failed
against GitHub. `fixtures/ledger/README.md` names two more traps in the captured
file specifically: gh returns label *objects* where `FakeGh` stores names, and
states are `OPEN`/`CLOSED` rather than lowercase. Concentrating the parse means
parity is one test module's standing obligation rather than a property every
caller has to remember.

### `FleetState` is derived and disposable

It carries only what a single snapshot cannot answer: per-agent idle timers and
the observation counters behind stall detection. It holds no authority — task
state lives in labels, ownership lives in the assignee, both on GitHub.

Losing it costs one thing: idle timers restart from zero, so a stall in progress
is detected up to `stall_minutes` late. That is the whole cost of a daemon
restart, which is what keeps parent §9's promise — the daemon holds no
authoritative state — true rather than aspirational. M3's restart test probes
exactly this.

### `expand.py` is pure and tested separately

Intents say what should happen; effects say which calls make it happen.
Splitting them keeps `decide()`'s tests readable as policy — they assert
`Dispatch(5, implementer)`, not a list of label strings — while still putting
the mutation sequence under test.

That sequence needs the coverage. `commands._post` today removes all eight state
labels and then adds one; `docs/m2-inputs.md` §5 notes that a failure between the
two strands a task with no state label at all, invisible to every label query.
`expand()` emits a state transition as a single combined edit, and a unit test
pins that it is one effect rather than two.

## 4. The purity seam

The parent design writes the signature as `decide(ledger, policy, agent_states)`.
Two inputs it needs are in none of those, and both were confirmed against the
captured fixtures rather than assumed:

- **Elapsed time.** Stall detection is "output unchanged for T minutes", but
  `fixtures/ledger/herdr-snapshot.json` carries no timestamp anywhere. Agents
  expose `agent_status`, `revision` and `state_change_seq` — monotonic counters,
  not clocks.
- **The agent → task link.** A herdr agent record carries `cwd`, `pane_id`,
  `workspace_id` and a terminal title. No issue number.

Resolution: the shell normalizes, and `decide()` becomes a fold.

```python
decide(prev: FleetState, ledger: list[Task], policy: Policy,
       agents: list[AgentState], now: datetime) -> tuple[list[Intent], FleetState]
```

The shell maps worktree paths to task numbers, stamps observations, and diffs
`state_change_seq` against `prev` to derive idle time. `decide()` receives
normalized `AgentState` records and threads its own memory explicitly.

Threading the state rather than hiding it in the shell is what makes a whole
night replayable: the harness runs tick after tick with no clock and no I/O,
feeding `now` from the scenario. A stall that takes eleven simulated minutes
costs no wall-clock at all.

The rejected alternative was letting the shell own the memory and hand
`decide()` a precomputed `idle_seconds`. It yields a simpler signature, but the
cross-tick bookkeeping then lives in code no unit test reaches — and cross-tick
bookkeeping is where the attempt counter, the fix-pass counter and the stall
timer all live. That is the wrong thing to leave untested.

## 5. What `decide()` decides

Parent §8's three phases, in order, once per tick.

**REAP** — for each CLAIMED task, read its agent state.

| Observation | Intent |
|---|---|
| clean exit | parse the packet, advance per its `to` |
| dirty exit, stalled (`idle ≥ stall_minutes`), or over per-task budget | `Release(task, reason)`, attempts += 1 |
| release would exceed `max_attempts` | `Escalate(task, attempts_exhausted)` → NEEDS_HUMAN |
| CLAIMED with no live agent | `Release` — ledger drift, herdr state is the tiebreaker |

**UNBLOCK** — BLOCKED tasks whose `blocked_on` task is DONE → READY. No human
is involved; this is the whole point of keeping `block` distinct from
`escalate`.

**DISPATCH** — while free slots and runnable tasks remain:

- skip any task not carrying `fleet:auto-ok`
- skip any task whose area is already held by a live task
- **stop entirely if any `class:integration` task is live**
- **stop dispatching at ≥ 80% of `fleet_usd`** — drain, do not kill; in-flight
  tasks finish

### Four rulings

**1. `decide()` gates on the `fleet:auto-ok` label alone, never on area.**

This settles `docs/m2-inputs.md` §2. `ownership.md` maps both `configs/**` and
`gates/**` to `area:configs`, while `gates/**` is separately non-dispatchable.
Issue #6 is legitimately `area:configs` *and* `auto-ok`, precisely because its
acceptance criteria forbid touching `gates/`. An allocator inferring
dispatchability from area would refuse #6 — which is one of the four hard fails
— or, with the inference inverted, permit a `gates/` edit.

Path-based non-dispatchability is enforced twice, and neither place is here: a
scout applies or withholds `fleet:auto-ok` at triage having read the criteria,
and the integrator rejects an out-of-bounds diff at merge. `decide()` reads the
label and asks no further questions. Written down here so M4 does not
re-litigate it.

**2. Two bounded counters, never one.** `fleet:attempt-N` counts dispatch
failures — stall, dirty exit, over-budget — and caps at `max_attempts` (2).
`fleet:fix-pass-N` counts `changes_required` rounds and caps at
`max_fix_passes` (1). A fix pass does not consume an attempt. This is harness
amendment 3, and it is the difference between two real tries and one.

**3. Integration takes a global exclusive lock.** While a `class:integration`
task is live, `decide()` dispatches nothing new. The integrator touches every
area by definition, so per-area exclusion does not cover it. Harness amendment
2; it costs throughput, and M5 measures how much.

**4. REVIEW exits only on a verdict.** Approved → INTEGRATING.
`changes_required` → back to CLAIMED with fix passes += 1, or to NEEDS_HUMAN if
the fix-pass cap is already spent.

Ruling 4 forces an input-shape decision. `FakeGh.list_issues` deliberately omits
comment bodies because `gh issue list` does not fetch them — a divergence found
and fixed in M1. So the shell issues `get_issue` for tasks in states that carry
a packet or a verdict, and `ledger.read()` populates those fields for those
tasks only. A `Task` whose state does not require a packet has `packet is None`,
and that is a fact about the query, not about the issue.

### The run log

Every intent and its outcome appends to a timestamped JSONL run log, per harness
spec §7.

It lands in M2 rather than with the grader, for three reasons. The fold produces
it naturally — `decide()` already returns exactly the list that needs recording.
Harness §7 makes it one of the three inputs `fleet grade` needs, alongside the
final ledger and `git log integration`. And `docs/m2-inputs.md` §3 records that
rubric 04 asserts `fleet:blocked` was applied, which the next transition removes
— so the ledger cannot answer "was it ever applied" and only the run log can.

Nothing reads it in M2. Writing it later would mean reconstructing a night that
has already happened.

## 6. The executor seam

```
decide(…) → [Intent] → expand() → [Effect]
                                     │
                 ┌───────────────────┴───────────────────┐
           FakeExecutor                            HerdrExecutor
     scripted outcomes, no I/O                  worktrees, agents, gh
                                                      (M3)
```

`FakeExecutor` scripts five outcomes per task: `clean_exit(packet)`,
`dirty_exit`, `stall`, `over_budget`, `conflict`. The state machine then runs
with no agents, no tokens and no network.

Building the seam now is nearly free because the parent design already made
`decide()` pure. Retrofitting it after a daemon exists is not.

## 7. Scenarios

One YAML file per claim, living beside the failure table it proves.

```yaml
name: poison-task-stops-at-two-attempts
proves: "parent §10 poison task; harness §7 hard fail 'no task exceeds 2 attempts'"
seed:
  ledger: fixtures/ledger/issues.json
  policy: fixtures/policy/fleet-fixture
ticks:
  - outcomes: {}                                    # nothing running yet
    expect: [Dispatch(5, implementer)]
  - outcomes: {5: dirty_exit}
    expect: [Release(5, dirty_exit)]
  - outcomes: {}
    expect: [Dispatch(5, implementer)]              # attempt 2
  - outcomes: {5: stall}
    expect: [Escalate(5, attempts_exhausted)]
  - outcomes: {}
    expect: []                                      # never a third attempt
```

The final tick asserts an *absence*, which is the shape of the claim: the
overnight failure being guarded against is not a wrong answer, it is a third
attempt that burns the night. A scenario that only asserted the escalation would
pass against an allocator that then dispatched again.

Seeding from `fixtures/ledger/issues.json` is deliberate and is why harness §5
put M2 after M0: the pure core is exercised against real captured ledger shapes,
not invented ones.

YAML rather than Python builders because a scenario is an artifact the spec
points at. A builder in the test suite is more flexible and type-checked, but
scenarios then drift from parent §10's table with nothing to notice.

### `fixtures/policy/fleet-fixture` is vendored, not referenced

A scenario needs a policy as well as a ledger, and M2 introduces a checked-in
copy of the fixture repo's policy layer — `fleet.toml`, `ownership.md`,
`task-classes.md` — captured the same way `fixtures/ledger/` captures gh output.

This also closes a wart `docs/m2-inputs.md` §5 raised: `tests/conftest.py`
currently points `FIXTURE_SCHEMAS` at `~/TIG/fleet-fixture/.agents/schemas`, an
absolute path into another repo's *working tree*, so the suite validates against
whatever happens to be checked out there. That is not hypothetical — merging
`fleet#1` required first committing a schema change that had been sitting
uncommitted in that tree, and until it landed the suite's 121 green tests were
reading a file no commit contained.

M2 vendors the schemas alongside the policy and points `conftest.py` at the
vendored copy. The cost is that a fixture-repo policy change must be re-captured
here deliberately, which is the same trade `fixtures/ledger/` already makes and
the same reason it is the right one: a fixture that silently tracks a live
working tree is not a fixture.

## 8. `fleet plan --dry-run`

Reads the live ledger through `RealGh`, runs one tick, prints the intents,
writes nothing.

"Writes nothing" is tested by wrapping the client in a decorator that raises on
every mutating method, so the guarantee is an assertion about the code path
rather than about having read it carefully. This is the same reasoning that made
`FakeGh` refuse unknown labels: a stand-in more permissive than the real thing
is how M1's defects survived.

## 9. Parsing the policy layer

`policy.py` reads `fleet.toml`, `ownership.md` and `task-classes.md`.

The two markdown files are tables, and the parser is strict: a malformed or
empty table raises rather than returning an empty mapping. A silently-empty
ownership map means no area exclusion, which means every task looks dispatchable
and the fleet runs six agents into one area — a failure that passes every test
not specifically looking for it. Same class as M1's fake/real divergences, and
the same fix: make the permissive path impossible rather than remembering not to
take it.

## 10. Testing

| Obligation | Shape |
|---|---|
| Parent §10's failure table | one scenario per row |
| Parent §11's cases | one scenario per case |
| Ledger parity | the same assertions over `FakeGh` output and captured `issues.json` |
| `expand()` | unit tests, including that a state transition is one effect, not a remove-then-add pair |
| Dry-run | provably writes nothing, via a write-refusing client |
| Policy parsing | a malformed table raises; an empty one raises |

The parity row is the one `docs/m2-inputs.md` §6 asks for by name: "does the fake
still behave like the real client" as a standing test obligation, not a review
finding. M2 is where that class of defect does its damage, because `decide()`
reads the ledger through exactly the surface that diverged four times.

## 11. Decisions and their reasons

| Decision | Reason | Rejected alternative |
|---|---|---|
| `decide()` is a fold | cross-tick memory is where the counters and timers live, and it must be testable | shell owns memory, hands over `idle_seconds` |
| Intent-level actions plus a pure expander | `decide()` tests read as policy; the mutation sequence is still covered | executor expands; label logic goes untested |
| Run log in M2 | the fold already returns it, and rubric 04 needs a label the ledger discards | defer with the grader, reconstruct later |
| `auto-ok` label only, never area | `configs` covers both dispatchable and non-dispatchable paths | infer dispatchability from area |
| YAML scenarios | a scenario is an artifact the spec points at | Python builders in the suite |
| Strict policy parser | an empty ownership map disables area exclusion silently | tolerate and return `{}` |
| Vendor the policy layer and schemas | a fixture tracking a live working tree is not a fixture; `fleet#1` shipped green against an uncommitted file | keep reading `~/TIG/fleet-fixture` directly |
| Three CLI items in scope, no more | each blocks the state machine; nothing else does | full `m2-inputs` sweep |

## 12. Open questions

1. **Does `now` belong in `FleetState` or stay a parameter?** Kept a parameter
   here, so a scenario can jump time without mutating state. If M3's daemon
   wants to record observation times, this may want revisiting.
2. **How does the shell map a worktree path to a task number?** M2 defines the
   normalized `AgentState` and lets the harness construct it directly; the real
   mapping is `HerdrExecutor`'s problem in M3. Deferred rather than guessed,
   because herdr owns the worktree naming.
3. **What is a per-task cost observation, concretely?** The budget rule needs
   dollars per task and nothing produces them yet. M2 models it as an input on
   `AgentState`; where the number comes from is M3's question.
4. **Does the global integration lock make most scenarios trivially serial?** If
   every scenario ends up single-threaded, the harness is not exercising area
   exclusion at all. Worth checking once the scenarios exist.
