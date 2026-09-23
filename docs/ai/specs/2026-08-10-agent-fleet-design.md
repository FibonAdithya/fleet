# Fleet: parallel agents with explicit handoffs

status: proposed
date: 2026-08-10
first consumer: `project-d`
companion: `~/TIG/project-h/ai-first-development-workflow.md`

## 1. What this is

`fleet` is a standalone tool that runs 3–6 coding agents concurrently against a
repository, unattended, and lets work move between them: an agent that finds an
unrelated bug files it for someone else, an implementer hands to a reviewer, a
large piece of work fans out and fans back in, and an agent that hits a wall
hands off rather than guessing.

The companion document describes a single agent's loop — context, plan, PR,
parallel *reviewers*, human gate, promotion. That loop is already implemented in
`project-d`. This design covers the layer it does not: many *implementation*
agents alive at once, and work changing hands between them.

### The problem being solved

Four agents already run by hand in herdr worktrees on `project-d`, against
seven open issues. Nothing coordinates them. The human is the scheduler, the
conflict arbitrator, the memory between sessions, and the only thing that
notices when an agent has been stuck for an hour. That does not survive going to
sleep.

### Goal

Seed a queue, start the fleet on `tig-server`, and find in the morning: work
merged into an integration branch with `make check` green, a small number of
tasks stopped at a specific question that needed a human, and no task that
burned six hours re-failing the same way.

### Non-goals

- **Not a replacement for herdr.** herdr owns worktrees, panes, and agent
  processes. `fleet` owns tasks, states and handoffs, and shells out to herdr.
- **Not an issue tracker.** GitHub Issues remains the tracker, as
  `project-d/AGENTS.md` already declares.
- **Not multi-repo in v1.** One consumer until one consumer works.
- **Not a merge authority.** Promotion to `main` stays human.
- **No scheduling strategy for slow-build repos.** `project-i` needs fewer,
  longer-lived tasks and a different concurrency model. Out of scope; see §12.

## 2. Architecture: four planes and a client

```
┌─ LEDGER (GitHub Issues) ────────────── authoritative · remote · durable
│  what work exists · state · owner · handoff history
└──────────────────────────────────────────────────────────────────────
        ▲ reads                                  ▲ writes (schema-checked)
        │                                        │
┌─ ALLOCATION (daemon, tig-server) ─┐     ┌─ EXECUTION (herdr) ─────────┐
│  decide() pure core               │────▶│  worktree + agent per task  │
│  executor · reaper · budget       │     │  writes via `fleet` CLI only│
│  sole writer of `owner`           │     └─────────────────────────────┘
│  state socket ─────────┐          │                    │ commits
└────────────────────────┼──────────┘                    ▼
        │ reads          │ attach/detach     ┌─ INTEGRATION ────────────┐
        ▼                ▼                   │  integration branch      │
┌─ POLICY (in target repo) ┐  ┌─ CLIENTS ─┐  │  → human → main          │
│  ownership map           │  │ status    │  └──────────────────────────┘
│  task classes            │  │ TUI       │
│  role prompts · schemas  │  └───────────┘
└──────────────────────────┘
```

### Ledger — GitHub Issues

Unchanged from what `AGENTS.md` already declares as the tracker. Labels carry
machine state, the assignee carries ownership, schema-checked comments carry
handoff packets. Nothing new to store and nothing to keep in sync.

Chosen over an in-repo git-based ledger (which would offer genuinely atomic
compare-and-swap via rejected non-fast-forward pushes) because a second tracker
beside an existing one is duplicated truth — the failure mode the companion
document names first. Issues also give a free audit trail and let a human
unblock a task from a phone at 2am, which matters more for unattended runs than
lock atomicity does. Atomicity is instead obtained structurally: see §5.

### Policy — in the target repo, versioned, reviewed like code

This half is what makes `fleet` a system co-designed with the repo rather than a
bot bolted onto it.

| File | Contents |
|---|---|
| `docs/agent/ownership.md` | path globs → area names |
| `docs/agent/task-classes.md` | which classes may run unattended, and where |
| `.agents/roles/*.md` | role prompts: scout, planner, implementer, reviewer, integrator |
| `.agents/schemas/*.json` | handoff packet and verdict schemas |
| `fleet.toml` | points the tool at the above; budgets; slot count |

### Allocation — one daemon

Reads the ledger, reads policy, decides, dispatches through herdr, waits,
records outcomes. It is the **only** writer of ownership. See §5.

### Execution — one herdr worktree per task

Agents never touch the ledger with free-form `gh` calls. They go through the
`fleet` CLI (`handoff`, `block`, `escalate`, `discover`), which validates
against the schemas before writing. This is the companion document's
"machine-readable verdicts, not grepping prose for MUST FIX", applied to
handoffs: a packet that does not validate never lands, so a receiving agent can
always parse what it is given.

### Clients — detachable

The daemon owns the loop and the state. Clients attach and detach over a state
socket. **If the TUI were the orchestrator, closing it would stop the fleet**,
which defeats unattended operation outright. Same shape herdr itself uses.

## 3. The task record

A task *is* a GitHub issue.

| Field | Where it lives | Written by |
|---|---|---|
| state | `fleet:ready` / `claimed` / `review` / `blocked` / `human` / `done` | allocator and agents (via CLI) |
| area | `area:eval`, `area:configs`, … | scout, from the ownership map |
| class | `class:patch` / `spec` / `investigation` / `integration` | scout |
| autonomous-ok | `fleet:auto-ok` | scout only |
| owner | GitHub assignee | **allocator only** |
| attempts | `fleet:attempt-N` | allocator |
| acceptance criteria | issue body, under a required heading | scout |
| handoff history | schema-checked comments | agents via CLI |

`fleet:auto-ok` encodes the "what requires a human" list already in
`project-d/AGENTS.md` — gate bands, variant semantics, pinned versions. An
issue touching those never receives the label and is never dispatched.

**An agent may not apply `fleet:auto-ok` to its own task.** This closes the loop
where completion pressure becomes self-authorization.

## 4. State machine

```
  TRIAGE ──scout adds area/class/criteria──▶ READY ◀──────────────┐
                                              │                   │
                          allocator assigns ──┤                   │ release
                                              ▼                   │ (stall/timeout/
                                          CLAIMED ────────────────┘  crash, attempt+1)
                                          │  │  │
                       ┌──────────────────┘  │  └──────────▶ BLOCKED ──▶ NEEDS_HUMAN
                       ▼                     │                            (terminal
                    REVIEW                   │                             until acted on)
                       │  changes_required   │
                       ├─────────────────────┘  (one bounded fix pass)
                       │ approved
                       ▼
                  INTEGRATING ──conflict──▶ back to CLAIMED for rebase
                       │ merged to `integration`, make check green
                       ▼
                     DONE
```

Two rules carry most of the weight.

**CLAIMED is the only state in which anything writes to a branch.** This is the
companion document's "give every state one writer", extended from one pull
request to the whole fleet. REVIEW is read-only. INTEGRATING is owned by the
integrator alone.

**Attempts are capped at 2.** After two failed passes a task goes NEEDS_HUMAN
and stops. Without this, an unattended run spends the night and a large token
budget re-failing one task.

## 5. Why there is no locking problem

The ledger is remote and offers no compare-and-swap, yet two agents can never
claim the same task, because **agents never self-assign**. Only the daemon
writes `owner` and `fleet:claimed`, and there is one daemon. A single writer
removes the race rather than resolving it.

The cost is a single point of failure. Accepted: the daemon holds no
authoritative state (see §9), so restarting it re-derives everything from the
ledger.

## 6. The handoff packet

The thing that travels. Written by the `fleet` CLI, validated against
`.agents/schemas/handoff.json`, posted as an issue comment.

```json
{
  "schema": "fleet.handoff.v1",
  "task": 21,
  "to": "review",
  "from": { "role": "implementer", "agent": "fleet-3", "run": "2026-08-10T02:14Z" },
  "base_sha": "fb5ec5d",
  "head_sha": "a91c204",
  "branch": "fleet/21-spectrum-reg-alpha",
  "did": "Raised spectrum_reg_alpha to 1.0; added a binding test at tests/train/test_spectrum_reg.py",
  "evidence": {
    "checks": "make check: pass",
    "commands": ["make check", "pytest -k spectrum"],
    "artifacts": []
  },
  "open_questions": ["Whether v1's band should be re-measured — needs a human"],
  "do_not": ["Do not change the gate band. Out of scope per issue #21."],
  "discovered": [24]
}
```

Field rationale, where it is not obvious:

- `base_sha` / `head_sha` — review verdicts bind to a SHA. A verdict that does
  not name the head it reviewed is not a gate.
- `did` + `evidence` — so the receiver does not re-derive what the sender
  already established.
- `do_not` — carries the **original** constraints forward, not just the previous
  agent's opinions. The companion document records a real incident where a fix
  agent created an unauthorized migration because it received reviewer comments
  without the task's original scope. This field is that lesson.
- `discovered` — links to issues filed under §7.1.

### The design test

**A handoff packet must be sufficient to resume the task with zero conversation
history.** If a receiving agent would need to read the sender's transcript, the
packet is underspecified and the schema needs another field.

This is the riskiest assumption in the design. It is tested in phase 1.5, before
any orchestration is built.

## 7. The four handoffs

### 7.1 Discovery → new issue

```
fleet discover --from 21 --title "..." --area eval \
               --evidence "src/eval/lid.py:44 rounds before clamping, so ..."
```

Creates an issue in **TRIAGE, never READY**, labelled `discovered-by:21`, and
appends its number to the parent's `discovered[]`. The agent then continues its
own task and does not touch the new issue.

Creating TRIAGE rather than READY is deliberate. An agent that could both file
and claim work has authorized itself, and overnight that compounds into scope
explosion. A scout pass promotes discoveries to READY.

Known abuse: filing a discovery to dodge a hard part of the actual task. Not
mechanically preventable; detectable, because a task that ends BLOCKED with
several discoveries and no commits is a visible pattern.

### 7.2 Stage relay, routed by class

A fixed five-stage pipeline for a one-line fix is waste. Class picks the route:

| class | route | output |
|---|---|---|
| `patch` | implementer → reviewer → integrator | merged commit |
| `spec` | planner | a spec document and child issues |
| `investigation` | implementer, forbidden from writing `src/` | findings comment |
| `integration` | integrator | `integration` advanced |

Each boundary is a handoff packet. A receiving agent's prompt is exactly: role
prompt + issue body + latest handoff packet + the repo's `AGENTS.md`. Nothing
else. The zero-history test of §6 is enforced by construction rather than by
discipline.

### 7.3 Fan-out and fan-in

Decomposition is itself a `class:spec` task: a planner reads an epic issue and
emits child issues, in distinct areas where possible. Children then run as
ordinary tasks.

When the last child reaches DONE, the allocator auto-creates a
`class:integration` task referencing the epic. The integrator merges the
children's work on `integration`, runs `make check`, and writes one combined
evidence bundle. Fan-out is a first-class task, not orchestrator magic.

### 7.4 Escalation, in two distinct flavours

```
fleet block    --on 22
fleet escalate --question "..." --considered "..."
```

- `block` → BLOCKED on another task. The allocator re-queues it automatically
  when 22 reaches DONE. **No human is involved.**
- `escalate` → NEEDS_HUMAN. Terminal until acted on. Requires a specific
  question and the options already weighed, not "I am stuck".

Keeping these distinct is the point. Collapsed into one state, a morning queue
fills with tasks that could have resolved themselves at 3am.

Both write a full handoff packet, so resuming is cheap.

## 8. The allocator loop

```
every 60s:
  REAP     for each CLAIMED task, read herdr agent state
             clean exit                      → parse packet, advance state
             dirty exit / stalled / over-budget → release, attempt += 1
             attempt == 2                    → NEEDS_HUMAN
  UNBLOCK  BLOCKED tasks whose dependency is DONE → READY
  DISPATCH while free_slots and runnable_tasks:
             pick highest-priority READY whose area is not currently held
             create worktree from `integration` HEAD
             start agent with (role prompt + issue + latest packet + AGENTS.md)
             assign owner, label claimed
```

Stall detection: `herdr agent read` output unchanged for T minutes while the
agent is nominally running. Most commonly an agent waiting at an interactive
prompt — so agents must be started non-interactive, and any tool that can prompt
is a hang waiting to happen.

### Collision avoidance, in three layers

1. **Area exclusion** (primary) — one live task per area, from the ownership
   map. Coarse on purpose: a coarse lock that can be reasoned about beats a fine
   one that deadlocks at 4am.
2. **Fresh base** — every worktree branches from current `integration`, never a
   stale `main`.
3. **Serialized integration** — exactly one `class:integration` task runs at a
   time, ever. Conflicts are always resolved by a single writer.

### The real concurrency ceiling

For `project-d` the realistic area count is about six: `eval`, `train`,
`models`, `data`, `configs+gates`, `docs`. **The ceiling is the disjoint-area
count, not the agent count.** At 3–6 agents the fleet is already at that ceiling
and the allocator will frequently hold idle slots. A seventh agent buys nothing.

If more parallelism is wanted later, the lever is splitting areas in the
ownership map — a repo-structure change — not raising a number in `fleet.toml`.

## 9. Integration and authority

`AGENTS.md` reserves merges for a human, correctly. But if five agents open pull
requests at 2am and nothing merges until morning, every agent after the first
builds on a stale `main`, in a ~70-file repo where they are already likely to
touch the same files. The morning result is five branches that each pass CI
alone and conflict with each other.

**Resolution: agents merge into a long-lived `integration` branch, never
`main`.** Conflicts surface at 2am, to an agent that can fix them, and each new
worktree branches from a fresh base. In the morning the human reviews one
combined evidence bundle and promotes `integration` → `main` in a single
decision. The authority rule is intact and staleness is solved.

Promotion remains human. So do the items already listed in `AGENTS.md`.

## 10. Failure modes

| Failure | Detection | Response |
|---|---|---|
| Silent stall | agent output unchanged for T min | release, attempt += 1 |
| Poison task | attempt counter | 2 attempts → NEEDS_HUMAN, stop |
| Runaway cost | per-task and fleet-wide budget | at 80%: **drain, do not kill** — stop dispatching, let in-flight finish |
| Conflict storm | integration fails twice on the same pair | both → NEEDS_HUMAN, stop integrating rather than thrash |
| Ledger drift | CLAIMED with no live agent | reconcile each loop; herdr state is the tiebreaker |
| Orphaned worktree | worktree for a non-CLAIMED task | reaper removes it |
| Duplicate discoveries | — | `fleet discover` searches open issues first; some duplicates accepted |

Three that need more than a table row.

### An agent weakening the gate to pass

Deleting a test, adding `|| true`, editing the Makefile. This is a **mechanical
guard in the integrator**: reject any diff touching `Makefile`,
`.github/workflows/`, `pytest.ini`, or with net test deletions, unless the issue
text explicitly authorizes it. A check, not a prompt instruction — a sentence in
a prompt is not branch protection.

The same reasoning makes `gates/`, `docs/agent/` and `.agents/` **structurally
non-dispatchable areas**: no task in them is ever `auto-ok`. `gates/` because
setting a band is a human judgement; the other two because they are the policy
files that govern the fleet, and an agent able to edit the ownership map or a
role prompt can widen its own authority. The tool's own source is out of reach
by construction, since it lives in a separate repository.

### Green CI but wrong

For `project-d` the sharp version is its first invariant: an agent
reporting "MMD improved, looks good", which `AGENTS.md` explicitly names as a
misreading of the project. One reviewer role therefore exists solely to check
the five invariants — the companion document's "domain specialist reviewer" with
a concrete, non-overlapping contract.

### The tig-server constraint

`tig-server` is 2 vCPU / 3.7 GB, no GPU, no Docker, no Rust. Worktrees will not
contain `data/*.npy` or `runs/`, which are gitignored artifacts. It can run
`make check` — ruff plus pytest, seconds, CPU-only, no dataset — and nothing
heavier.

**Overnight on tig-server means code, tests, configs and docs, verified by `make
check`.** Anything requiring a training run or a real dataset is never
`auto-ok` there and routes to `tig-gpu` or the laptop. This belongs in
`task-classes.md` explicitly, because it is the rule an agent is most likely to
violate cheerfully.

## 11. Testing the fleet

The companion document's closing principle is that an AI workflow is production
software. One decision makes that practical here.

**The allocator is a pure function.** `decide(ledger, policy, agent_states) →
[actions]`, with no network, no herdr, and no side effects. A thin imperative
shell executes the returned actions. Everything interesting becomes unit
testable against fixtures — real captured `gh issue list --json` and `herdr api
snapshot` output, checked into the repo.

Cases that earn their keep:

- two READY tasks in one area → exactly one dispatched
- stalled agent → released, attempt incremented
- second failure → NEEDS_HUMAN, not a third attempt
- blocked task's dependency closes → requeued with no human involvement
- last epic child closes → integration task auto-created
- budget exhausted → drains, does not kill
- CLAIMED task with no live agent → released

Plus schema round-trip tests on handoff packets, where malformed input must be
rejected, and tests on the integrator guard checks — those are security
controls, not conveniences.

Two operational modes before this is ever pointed at real work: `fleet run
--dry-run` prints the action plan and touches nothing, and a rehearsal against a
scratch clone seeded with fabricated issues.

### The daemon holds no authoritative state

It keeps a cache of the ledger for low-latency rendering. **Every write goes to
GitHub first; the cache is derived and disposable.** If it is lost, rebuild from
`gh`. The TUI never writes state directly — it issues the same commands the CLI
does.

## 12. Build phases

| Phase | What | Autonomous execution |
|---|---|---|
| 0 | policy files, labels, hand-triage the 7 open issues | none |
| 1 | `fleet` CLI + schemas | none |
| **1.5** | **two-agent handoff trial** | supervised |
| 2 | `decide()` pure core, `fleet plan --dry-run` | none |
| 3 | daemon + executor + state socket + `fleet status` | supervised |
| 4 | roles, integration branch, integrator guards | yes |
| 5 | TUI as a socket client | — |
| 6 | `fleet init`, second repo | — |

**The first implementation plan covers phases 0, 1, 1.5 and 2 only.** Phases 3–6
wait until the phase 1.5 trial has said whether the packet schema survives
contact with two real agents. Planning an orchestrator around a handoff format
that has not been tried is planning the wrong orchestrator.

### Phase 0 is useful standalone

The ownership map, task classes, labels and a triaged backlog improve hand
dispatch even with zero automation, and the triaged issues become the fixtures
for phases 2 and 3.

### Phase 1.5 is the point of the ordering

Phases 0–2 involve no autonomous execution, and phase 3 — the riskiest code — is
deliberately the thinnest thing in the system. But the riskiest *assumption* is
not in any phase: it is the zero-history handoff of §6.

So the first real move is smaller than a phase. Do phases 0 and 1 for a single
area, then hand-run two agents on one real issue with a packet between them.
Agent A implements and writes a handoff; agent B reviews from the packet alone,
with no access to A's session. If B flounders, the design is wrong and that is
known in an afternoon rather than after an orchestrator exists. If B works
cleanly, everything downstream is mechanics.

### Deferred deliberately

- **`fleet init`.** It can scaffold empty files and infer areas from directory
  structure. It cannot write "ANN-difficulty is the gate; MMD is a diagnostic",
  or decide that `gates/` is never dispatchable. Those are judgements about a
  specific codebase and they are the whole difference between a fleet that does
  useful work and one that confidently does the wrong thing. The risk is not
  that `init` fails but that it emits something plausible that gets trusted. It
  lands after the policy work has been done by hand once, and it interviews
  rather than guesses.
- **A second repo.** Generalizing before one consumer works multiplies the cost
  of being wrong about §6.
- **`project-i`.** Seconds-long `make check` is what makes fine-grained
  parallel tasks viable. A Rust workspace with slow builds and a shared
  `Cargo.lock` wants fewer, longer-lived tasks. That is a second scheduling
  strategy, not a configuration knob.

## 13. Decisions and their reasons

| Decision | Reason | Rejected alternative |
|---|---|---|
| GitHub Issues as ledger | already the tracker; free audit trail; reachable from a phone | in-repo git ledger with push-based CAS — atomic, but duplicated truth |
| Single-writer allocation | removes the race instead of resolving it | per-agent self-claiming with distributed locks |
| Area exclusion as the lock | reasonable at 4am | file-level locking; deadlock-prone |
| `integration` branch | conflicts surface to agents, not to the morning | PRs against `main`, accept staleness |
| Standalone tool, one consumer | zero extraction cost later, no premature generality | build inside `project-d` |
| Daemon + detachable clients | closing the TUI must not stop the fleet | TUI as orchestrator |
| Pure `decide()` | the interesting logic is testable without running an agent | logic in the executor |
| Attempt cap of 2 | bounds the cost of a poison task overnight | unbounded retry |

## 14. Open questions

1. **Stall threshold T.** Unknown until observed. A `make check` run is seconds,
   but an agent thinking is legitimately quiet for minutes. Start at 10 minutes,
   instrument, tune.
2. **Does area exclusion leave the fleet mostly idle?** Six areas and six slots
   with dependencies between them may mean two agents run most of the night.
   Phase 3 should log slot utilization so this is measured, not guessed.
3. **Does the scout role justify its cost?** Triage may be cheap enough to stay
   human, at least until the discovery rate is known.
4. **Where do review verdicts live** — the existing PR review workflow, or the
   fleet ledger? Overlaps with `project-d`'s existing Claude review
   workflow, which is already running and must not be duplicated.
5. **Integration-branch lifetime.** Reset after each promotion, or long-lived?
   Reset is cleaner; long-lived survives an unpromoted morning.
