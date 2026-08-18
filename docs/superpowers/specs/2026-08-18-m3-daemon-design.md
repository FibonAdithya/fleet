# M3: the daemon, the real executor, and the first vertical slice

status: proposed
date: 2026-08-18
parent designs: `2026-08-10-agent-fleet-design.md`, `2026-08-10-fixture-harness-design.md`
predecessor: `2026-08-17-m2-allocator-design.md`
inputs: `docs/m2-inputs.md`

## 1. What this is

M2 built `decide()` and proved it decides. Nothing runs. There is no process
that ticks, no code that starts an agent, and no way to look at a fleet that is
working — `fleet plan --dry-run` reads the ledger once, prints intents, and
exits, with `--no-dry-run` refused in as many words.

M3 is the imperative shell around the tested core: a daemon that ticks, a
`HerdrExecutor` that creates worktrees and starts real agents, a state socket
with `fleet status` as its first client, the `integration` branch, and an
integrator that merges. The deliverable is one issue driven from READY to DONE
by the machine, and a daemon that can be killed in the middle of it.

### Goal

Issue 1 of `fleet-fixture` goes READY → CLAIMED → REVIEW → INTEGRATING → DONE
with no human performing a transition, its work merged into `integration` with
`make check` green — and the daemon killed mid-run and restarted without the
fleet losing track of what it was doing or how much it had spent.

### Non-goals

- **No integrator guards.** The diff checks in `integrator.md` step 2 stay
  prompt-only. M4. See §6, which states the consequence rather than burying it.
- **No role routing beyond the `patch` route.** `spec`, `investigation` and the
  invariant reviewer are M4.
- **No epic fan-in.** `m2-inputs.md` §7 offers back the two intents M2 removed;
  §9 declines them, with reasons.
- **No grader.** The run log gains a field; nothing reads it yet.
- **No TUI.** Parent phase 5 stays deferred; `fleet status` is a one-shot client
  built for `watch`.
- **No rubric rework.** Still a fixture-validity problem, still not an allocator
  problem.

### Scope taken on beyond the harness spec's M3 row

The fixture reset (§8). The harness spec's M3 gate has to be run at least twice
— once for the slice, once for the restart — and `docs/m2-inputs.md` §4 records
that the fixture is single-use: `seed_issues.py` is idempotent by title, so run
2 finds eleven issues still carrying `fleet:done` and an empty backlog. A gate
that cannot be run twice is not a gate.

## 2. What M3 inherits, and what it discovers

Six items arrive as debt from `m2-inputs.md` §7. Each was written down there as
inert in M2 and live in M3, and each is live for the same reason: **M3 is the
milestone in which issues get closed and packets get fetched.** Both were
impossible before — `close_issue` exists on `FakeGh` alone, and `do_plan` passes
no `packets` argument at all — and both become ordinary on day one.

| Inherited | Becomes live because | Resolved in |
|---|---|---|
| `_open` narrows UNBLOCK | completion closes issues, so a satisfied dependency is a closed row | §5.3 |
| `_open` leaks a closed CLAIMED row's agent | same | §5.3 |
| A stale packet advances a re-dispatched task | the shell fetches handoff comments off an issue that outlives the attempt | §5.5 |
| `Assign` has no executor | the executor is the thing that knows the fleet's identity | §4.3 |
| `integrator → claimed` reads as drift | an integrator exists to hand back | §5.4 |
| Ledger query must be `all`, not `open` | fetching packets without it re-wedges UNBLOCK from the query side | §4.1 |

A seventh is not in `m2-inputs.md`. It was found reading `_reap` against §5.1's
new route depth and it is the sharpest of the set: **an agent that has already
handed off is never stopped.** See §5.2.

## 3. Architecture

```
       tig-server ─────────────────────────────────────────────────────
        │
        ├─ fleet run  (daemon, cwd = ~/TIG/fleet-fixture)
        │    │
        │    │  every poll_seconds:
        │    │    Ledger ──── gh issue list --state all  ──┐
        │    │    Herdr  ──── herdr api snapshot ──────────┤
        │    │    Cost   ──── ~/.claude/projects/*.jsonl ──┤
        │    │                                             ▼
        │    │                            decide(prev, ledger, policy, agents, now)
        │    │                                             │ intents
        │    │                                  runlog.append(each)
        │    │                                             │
        │    │                               expand(intent, policy) → effects
        │    │                                             │
        │    │                            HerdrExecutor.apply(effects)
        │    │                                             │
        │    └─ publish snapshot ──▶ state socket ◀──── fleet status
        │
        └─ herdr server ─── worktrees ─── claude agents ─── fleet CLI ──▶ gh
```

Steps between `decide` and `expand` are M2's, unchanged. Everything M3 adds is
on the two edges: the three readers above `decide`, and the executor below
`expand`.

### The shell is collaborators, not one class

`HerdrExecutor` is the only new thing implementing M2's `Executor` protocol, and
it composes four units that each own one dependency:

| Unit | Owns | Faked by |
|---|---|---|
| `Herdr` | `herdr api snapshot`, worktree create/remove, agent start/prompt/stop | `FakeHerdr`, seeded from a captured snapshot |
| `Cost` | session UUID → transcript → priced tokens | pure; tested against a captured transcript |
| `Ledger` (extended) | `gh` reads including handoff comments | `FakeGh`, extended |
| `Socket` | the state socket server | a client test over a real socket in a tmpdir |

The alternative — one `HerdrExecutor` owning all four dependencies — was
rejected because it makes the pricing untestable without also standing up a fake
herdr, and pricing is the part most likely to be quietly wrong. `Cost` as a pure
function over a transcript and a price table is a unit test with no I/O in it.

Dropping M2's `Executor` protocol and letting the daemon wire herdr, git and gh
directly was rejected outright: that seam is what `FakeExecutor` and all seven
scenarios stand on, and they are M2's entire deliverable.

### Where it runs

`tig-server`. 2 vCPU, 3.7 GB, no GPU, no Docker — the box parent §10 describes,
and the box M4's unattended run will use. A `fleet-fixture` clone lives there
with `gh` authenticated and the runbook's HTTPS credential helper; `herdr` runs
there; the daemon's cwd is the clone, so it finds `fleet.toml` and the policy
layer exactly as `fleet plan` does today.

Developing against a local herdr and deploying to `tig-server` at M4 was
rejected for one reason: it would leave the 2 vCPU / 3.7 GB ceiling unmeasured
until the first unattended run, which is the run where being wrong is expensive.
Whether that box can hold six concurrent Claude agents is a fact M3 should
produce, not a question M4 should discover.

## 4. The tick

```
1.  ledger  = Ledger.read(gh.list_issues("all"), packets=fetch_for(live_states))
2.  agents  = Herdr.agents()            # snapshot ∘ task identity ∘ Cost
3.  intents, state = decide(prev, ledger, policy, agents, now)
4.  for intent in intents: runlog.append(intent, cost=final_cost_if_reaping)
5.  effects = [e for i in intents for e in expand(i, policy)]
6.  executor.apply(effects)
7.  publish(snapshot_of_this_tick)
```

### 4.1 The ledger query is `all`

`do_plan` currently calls `gh.list_issues("open")`. M3 must not. Once the shell
fetches packets, `Task.blocked_on` is populated and UNBLOCK becomes reachable —
and an `open`-only query drops the closed dependency that would satisfy it,
reintroducing from the query side exactly the wedge §5.3 removes from the filter
side. The two halves are one fix and land together.

Packets are fetched per task, not per ledger: `gh issue view N --json comments`
for the tasks in a live state, since `gh issue list` returns no comment bodies.
That is the same fact `Task.packet`'s docstring already states — `None` is a
fact about the query, not about the issue.

### 4.2 Agent identity is a branch name

herdr's snapshot carries no issue number. A task's agent is found by parsing
`fleet/<n>-<slug>` out of the workspace's `worktree.checkout_path`; the number
before the first dash is the task.

This is what makes the restart test passable rather than aspirational. Nothing
in the daemon's memory is required to answer "who is working on #1" — the
answer is a branch name on disk plus a label on GitHub, and both survive `kill
-9`.

### 4.3 `Assign` finally has an executor

`expand()` emits `Assign` on `Release` and `Escalate` with `owner=None`, and M2
had no `GhClient` method to carry it out. M3 adds `assign`, and `Dispatch`'s
expansion gains the other half: an `Assign` naming the identity `gh` is
authenticated as. Parent §3 makes the allocator the sole writer of ownership;
until now it wrote none.

`close_issue` graduates from `FakeGh`-only to the `GhClient` Protocol at the
same time. A fake able to perform a transition the real client cannot is the
exact divergence class `m2-inputs.md` §6 says did all the damage in M1.

### 4.4 Starting an agent

`StartAgent(task, role, base)` becomes:

1. fetch `base` (the integration branch) — the fresh-base rule of parent §8.2 is
   a fetch, not a sentence;
2. `herdr worktree create --branch fleet/<n>-<slug> --base <base>`;
3. export `FLEET_ROLE` and `FLEET_AGENT` into the pane — the runbook is explicit
   that an unset `FLEET_AGENT` silently attributes packets to `manual`, which is
   indistinguishable from a human at a keyboard;
4. `herdr agent start --kind claude`, with permissions pre-granted;
5. `herdr agent prompt` with exactly role prompt + issue body + latest packet +
   `AGENTS.md`, and nothing else, per parent §7.2.

Step 4's parenthetical is a failure mode, not a convenience. Parent §8 names an
agent waiting at an interactive prompt as the most common cause of a stall; a
permission prompt is a hang that costs `stall_minutes` and one attempt.

`StopAgent` closes the pane and removes the worktree, which also makes parent
§10's orphaned-worktree row coverable for the first time: a worktree whose task
is not in a live state is now both visible and removable.

### 4.5 Cost

`price(transcript, prices) -> float`, pure. herdr's `agent_session.value` is the
Claude session UUID; the transcript is `~/.claude/projects/*/<uuid>.jsonl`.
Verified against the captured snapshot: 10 of 10 agents resolved to a transcript.
Each assistant message carries `input_tokens`, `output_tokens`,
`cache_read_input_tokens` and `cache_creation_input_tokens`; `fleet.toml` gains
a `[prices]` table keyed by model, in dollars per million tokens.

**A model with no price entry is an error, not zero.** Pricing an unknown model
at zero switches the budget ceiling off silently, and the event that triggers it
— a model changing under an agent — happens without anyone editing `fleet.toml`.
`load_config` cannot catch it, because the model name arrives at runtime from a
transcript.

The response is to drain: in-flight agents finish, nothing new dispatches, and
the socket reports the unpriced model as the reason. That is the same response
parent §10 already prescribes for a budget the fleet cannot honour — "drain, do
not kill" — and it is the only one of the three candidates that is neither
running blind (price it at zero) nor throwing away paid-for work (exit).

A new attempt gets a new worktree and therefore a new session UUID, so per-agent
cost is per-attempt by construction.

## 5. Changes to the pure core

Five. Four are forced by §2's inheritance; one is the discovery.

### 5.1 Dispatch reads the route by position

`_dispatch` skips every task whose state is not `ready` and always dispatches
`route[0]`. So the later roles of `patch` — implementer → reviewer → integrator
— are unreachable: a task at `fleet:review` is never handed to a reviewer, and
`fleet verdict` moving one to `fleet:integrating` hands it to nobody. Both
states are in `_LIVE`, so such a task holds its area and its slot for the rest
of the night. `m2-inputs.md` §7 records this as a consequence a scenario author
has to know; at M3 it is the difference between the slice reaching DONE and not.

The mapping:

| State | Role |
|---|---|
| `ready` | `route[0]` |
| `review` | `route[1]` |
| `integrating` | `route[-1]` |

A route too short for the state escalates — never an `IndexError` out of the
pure core, which takes down every other task's intents with it. `_unroutable`
widens to carry this: from "READY and `auto-ok` with no route at all" to "in any
dispatchable state, with no role for that state". Its existing reason string
stops being sufficient, so the two cases are named apart — `unroutable` for a
missing class, `unroutable_stage` for a route that has no role for the state the
task reached. A task that reaches REVIEW under a route with no reviewer is a
policy error, and the run log should say which policy error.

`route[-1]` rather than `route[2]` because M4 adds the invariant reviewer and a
four-role patch route still ends at the integrator. M4 will still edit this
table: an invariant-review stage needs a state label of its own. Three explicit
rows are a better thing to edit than a positional assumption.

**A continuation is not new work, and `_dispatch` has to stop treating it as
such.** `review` and `integrating` are in `_LIVE`, so a task in either state
already holds its area and its slot — *itself*. Left as written, every one of
`_dispatch`'s three guards refuses it: its area is held (by it), a slot is
consumed (by it), and at six live tasks `free` is zero. The task would never
reach its reviewer, which is the deadlock this section exists to remove.

So `_dispatch` runs in two passes. Continuations first — tasks at `review` or
`integrating`, matched against the areas and agents of *other* live tasks, and
not charged a slot, because the slot is one they already hold. New work second,
from READY, under the existing area, slot and budget rules. Splitting the passes
is also what keeps the deadlock impossible rather than merely unlikely: a full
fleet can always finish what it started.

**The integration lock widens with them.** M2 keys it on `klass == "integration"`,
which was complete when nothing could dispatch an integrator any other way. It
no longer is: a `class:patch` task at INTEGRATING has an integrator agent writing
the `integration` branch, and two of those at once is precisely the collision the
lock exists to prevent. The lock reads `klass == "integration" or state ==
"integrating"`, and — like the area check — excludes the candidate itself.

M2's existing division of the lock's two halves is preserved exactly, because
`integration-lock.yaml` pins it: a live integrator blocks all *new* dispatch,
while one dispatched by this tick blocks only other integrators in the same tick.

### 5.2 An agent that has handed off is never stopped

Not in `m2-inputs.md`. `_reap` (`decide.py:146`) skips every task whose state is
not `claimed`. But an implementer ends its stage by running `fleet handoff --to
review`, which moves the label *itself*, and then exits. On the next tick the
task is `review`, REAP passes over it, and the exited agent is never stopped.

Three consequences compound. The dead agent stays in the herdr snapshot, so
`_dispatch`'s `busy` set (`decide.py:250`) still counts it and the task can
never be dispatched to a reviewer. Its slot is held for the night. And its
worktree is never removed.

M2 never saw it because no simulation ever ran a second stage — the same
blind spot `m2-inputs.md` §7 records as "no scenario drives an approval or a fix
pass".

The fix is a new intent:

```python
@dataclass(frozen=True)
class Retire:
    task: int
```

expanding to `StopAgent` + `Assign(owner=None)` and nothing else. It changes no
state, because the agent already wrote the state; it joins `_REAPING`, so the
retired agent's cost is finalised into `spent_usd` rather than being lost. REAP's
loop widens from `claimed` to all three live states, and a `done` agent on a
task that is no longer `claimed` retires instead of advancing.

The retired agent is still in this tick's snapshot, so the next stage is
dispatched on the **following** tick, once herdr stops reporting it. One
`poll_seconds` per stage boundary is the cost; making it same-tick would mean
`_dispatch` reading `_reap`'s intents, and a dispatch that depends on the order
of phases within a tick is a worse thing to own than a minute of latency.

**A reviewer or integrator that stalls or exits dirty escalates.** REAP's
existing `_fail` is wrong for these states: `Release` relabels to READY, which
would silently restart a task from its implementer and throw away a review that
has already happened. `Escalate` is bounded, terminal, and honest — a stage that
failed after the work was done is a human's problem. Without this rule the
widened loop notices a stalled reviewer and does nothing about it, and the task
holds its slot for the rest of the night, which is the defect §5.2 exists to
remove rather than relocate.

### 5.3 The `_open` filter splits three ways

One blanket call shared by three phases cannot be right, because the three
phases disagree about what a closed row means:

| Phase | Sees | Because |
|---|---|---|
| REAP | every row | a CLOSED row still labelled `fleet:claimed` must be released and its agent stopped; skipping it leaks a live agent whose cost still counts against the ceiling and whose area a second agent then enters |
| UNBLOCK | open rows, `done` computed over every row | a closed issue **is** a satisfied dependency, whatever label it still carries |
| DISPATCH, `_unroutable` | open rows | unchanged — a closed issue is not work |

`_unblock` becomes `_unblock(open_ledger, ledger)` with
`done = {t.number for t in all_rows if t.state == "done" or not t.is_open}`,
which is the change `m2-inputs.md` §7 writes out verbatim.

### 5.4 The rebase hop does not burn an attempt

`integrator.md` step 3 hands a task back to CLAIMED on conflict. Nothing starts
an agent at that moment, so the next tick reads it as drift and `_reap` releases
it with `attempt + 1` — inflating `fleet:attempt-N`, which is exactly what the
grader's "no task exceeds 2 attempts" hard fail reads. A task could reach
NEEDS_HUMAN for having conflicted twice.

Fixed here rather than by adding `ready` to the fixture repo's handoff enum, so
the captured policy fixtures keep their fidelity. REAP recognises a CLAIMED task
whose newest packet is an integrator handback and emits
`Release(task, reason="rebase", attempt=task.attempts)` — the same number, so
the label does not advance — and DISPATCH picks it up from READY as normal. No
new intent type; the distinction lives in the reason, which is also what the run
log records.

**A rebase that costs no attempt has to cost something else, or it loops.** An
integrator that conflicts, hands back, gets re-dispatched, and conflicts again
would cycle forever on a counter that never moves — burning the fleet's budget
on one task all night, which is the failure the attempt cap exists to prevent.
Parent §10 already names the bound: "integration fails twice on the same pair →
both NEEDS_HUMAN, stop integrating rather than thrash".

So a third bounded counter, in the shape the harness spec's amendment 3 already
established for fix passes: `fleet:rebase-N`, `max_rebases` in `fleet.toml`
(default 1), counted separately from attempts and from fix passes, and exceeding
it escalates. Three counters is one more than the harness spec's "two bounded
counters, not one", and the reason is the same reason it gave for splitting the
first two — three distinct kinds of failure sharing one cap is the difference
between a real retry and none. Dispatch failures, review fix passes and rebases
are answers to three different questions and no cap can mean all three.

This is also what makes parent §10's conflict-storm row a rule rather than a
description: the storm is bounded by `max_rebases`, and `Task` gains `rebases`
alongside `attempts` and `fix_passes`.

One operational consequence, because M1 already paid for this lesson: the label
vocabulary is sized from the caps, and the runbook records that `gh issue edit`
fails the *whole* edit on a label the repo does not have. So `fleet:rebase-N`
has to be created by the seeder before any tick can write it, and the reset in
§9 has to know about it.

### 5.5 A packet is bound to its attempt

The shell fetches the newest handoff comment off an issue that outlives the
attempt, so attempt 1's packet is readable on attempt 2 and would advance work
that never happened — a task released for a dirty exit, re-dispatched, and then
advanced to REVIEW on the previous attempt's evidence.

`handoff.schema.json` gains a required `attempt`, and REAP refuses a packet
whose attempt does not match the task's `fleet:attempt-N`, treating it as no
packet at all.

The alternative — filter comments by timestamp against the attempt's dispatch
time — was rejected because that time lives only in the run log, and the run log
is explicitly disposable. The filter would fail *open* after a restart, on
precisely the path a restart is most likely to disturb.

This is the one change that edits `~/TIG/fleet-fixture`, and the vendored copy
under `fixtures/policy/fleet-fixture/.agents/schemas/` must be re-captured from
it rather than hand-edited, or the vendored policy stops being a capture.

## 6. Integration, and what "minimal integrator" leaves out

`integration_branch` is already in `fleet.toml` and `Policy` refuses to be built
without it. The branch itself does not exist in `fleet-fixture` and is created
off `main`.

A task at `fleet:integrating` is dispatched to an agent running `integrator.md`:
it merges into `integration`, runs `make check`, and hands to `done` — the one
role permitted to reach that state, bound by `FLEET_ROLE` since M2 Task 7.
Reaching `done` now also closes the issue, via a new `CloseIssue` effect. That
is what makes §5.3's UNBLOCK fix load-bearing rather than theoretical: closed
becomes the ordinary end state the same day the filter learns to read it.

**What M3 does not add is step 2 of that prompt** — the mechanical guard
rejecting a diff that touches `Makefile`, `.github/workflows/`, `gates/`,
`docs/agent/` or `.agents/`, or that deletes tests net. The harness spec is
explicit that M4 adds the guards, not the merge. Stated as a consequence rather
than left in a phase table: **issue 6 is unprotected in M3.** Parent §10 says a
sentence in a prompt is not branch protection, and in M3 a sentence in a prompt
is all there is. Issue 6 must not be in the backlog for M3's slice, and the
first run that includes it is an M4 run.

## 7. The state socket

A Unix domain socket, newline-delimited JSON, one request in M3: give me the
last completed tick. The response carries tick number and time; a row per task
with state, role, agent, attempt, cost and age; spend against the ceiling; free
slots; held areas; and whether the fleet is draining.

The tick publishes by swapping one reference to a frozen snapshot, so the server
thread needs no lock and can never observe a half-written tick.

`fleet status` connects, prints a table, and exits — built for `watch fleet
status`, per harness §7, and deliberately not a TUI. If the daemon is not
running the client says so and exits non-zero, rather than rendering state from
the ledger and leaving a reader unable to tell a dead fleet from an idle one.

Nothing authoritative lives here. The socket serves a render of a tick; losing
it costs nothing, which is the same promise parent §9 makes about the cache and
§9 below makes about `FleetState`.

## 8. Restart, and the thing that does not survive it

Parent §5 accepts a single point of failure only because §9 promises the daemon
holds no authoritative state. `FleetState` carries two fields and they do not
behave the same way under a restart.

`seen` resetting is already priced: `model.py:69` records that idle timers
restart, so a stall in progress is noticed up to `stall_minutes` late. That is
the whole cost, and it is acceptable.

`spent_usd` resetting to `0.0` is not. It means **a restarted daemon forgets the
night's spend and the fleet ceiling stops binding** — a 2am restart hands the
fleet a second full budget, and the drain rule that parent §10 calls "at 80%:
drain, do not kill" never fires. The daemon is designed to be restartable, so
this is not an edge case; it is the documented recovery path.

The run log is append-only and already records every intent. M3 records each
reaped agent's final cost on its intent line and re-derives `spent_usd` by
summing the log at startup. Still derived, still disposable — but reconstructible,
which is what §9's promise actually requires and what "holds no authoritative
state" was always shorthand for.

The restart half of the gate therefore asserts two things, not one: the fleet
re-derives what it was doing, and `spent_usd` after the restart equals what it
was before. Without the second, "survives a restart" is true of the state machine
and false of the budget ceiling, which is the half that costs money.

## 9. Fixture reset

`docs/runbook.md` already documents the reset as a sequence of hand-run
commands, and `docs/m2-inputs.md` §4 records that it does not actually reset:
`seed_issues.py` is idempotent by title, so run 2 skips all eleven issues and
never restores their labels. Run 1's `fleet:done` and `fleet:human` survive, and
run 2's allocator sees an empty backlog.

M3 turns the runbook's steps into a script that: restores issues 1–11 to their
seeded labels; closes everything numbered above 11; deletes `discovered-by:*`
labels; deletes run comments; resets `integration` to `main`; and removes
`fleet/*` branches and worktrees.

It also fixes `do_discover`, which searches `list_issues("all")` while its
docstring says "an open issue". Once run 1 files a discovery, every later run's
`discover` exits 1 and rubric 08 fails for a reason having nothing to do with
the orchestrator — a fixture defect that would be graded as an allocator defect.

## 10. Testing

M2's seam is the contract. `FakeExecutor` and all seven scenarios must keep
passing untouched; that is the regression signal that §5's changes did not
reshape the core underneath them.

`m2-inputs.md` §6's lesson becomes a standing obligation rather than a review
finding. Four `FakeGh`/`RealGh` divergences were found in M1 and every one would
have passed the entire offline suite and failed against GitHub. So every fake M3
adds is tested against a captured real artefact:

| Fake | Captured against |
|---|---|
| `FakeHerdr` | a real `herdr api snapshot` taken with a `fleet/<n>-` worktree live |
| `Cost` | a real Claude transcript, checked in, with its token counts |
| `FakeGh` (extended) | real `gh issue view --json comments` output |

Two failure rows M2 documented as uncoverable become coverable, and both get
scenarios. **Orphaned worktree**, because M3 is the first milestone with
worktrees to orphan. And **conflict storm**, but not for the reason
`m2-inputs.md` §7 predicted: it guessed the row needed an executor that could
report a conflict as distinct from a dirty exit. It does not. A conflict is
reported by the integrator agent, as a handback packet, and the row is a rule
about how many handbacks are tolerated — so §5.4's `max_rebases` is the whole
coverage, and it is testable against `FakeExecutor` with no real executor at
all. The prediction was wrong in a useful direction: the row was blocked on a
missing *rule*, not a missing *signal*.

New unit coverage, each named against the mutation it catches:

- route-position dispatch: a task at `review` dispatches the reviewer; a `spec`
  task at `review` escalates rather than raising
- `Retire`: a `done` agent on a task at `review` is stopped, its cost finalised,
  and the task is dispatched on the next tick; a stalled agent on a task at
  `review` escalates rather than releasing
- the filter split: a CLOSED `fleet:claimed` row is released and its agent
  stopped; a closed dependency satisfies UNBLOCK; a closed row is not dispatched
- rebase: an integrator handback releases at the *same* attempt number, and the
  handback after `max_rebases` escalates instead of releasing
- stale packet: attempt 1's packet does not advance attempt 2
- pricing: an unknown model raises; a known one matches a hand-computed figure
- `spent_usd` rebuilt from a run log equals the live value

## 11. Decisions and their reasons

| Decision | Reason | Rejected alternative |
|---|---|---|
| Daemon on `tig-server` | M3 should produce the 2 vCPU ceiling as a fact, not leave it for M4 to discover | develop locally, deploy at M4 |
| Collaborators behind M2's seam | pricing is testable with no I/O; the seam the scenarios stand on is untouched | one fat `HerdrExecutor`; or drop the protocol |
| Dispatch all three route positions | REVIEW and INTEGRATING otherwise hold an area and a slot forever | automate the implementer only, human verdict and merge |
| Cost from transcript tokens × price table | exact per-attempt attribution, no extra process on a small box | OTEL collector; token-denominated budget; defer to M4 |
| Unknown model raises | a zero price turns the ceiling off silently | default to zero |
| `attempt` in the handoff schema | self-describing; survives a restart | timestamp filter, which fails open once the run log is gone |
| Rebase fixed in REAP | keeps the vendored policy capture faithful | add `ready` to the handoff enum |
| A third counter, `max_rebases` | a hop that costs no attempt loops forever otherwise; it is also parent §10's conflict-storm bound | let rebases consume an attempt, which reintroduces the grader defect §5.4 exists to remove |
| `spent_usd` re-derived from the run log | the ceiling must bind across the recovery path the design depends on | persist `FleetState`, which makes it authoritative |
| Fixture reset in scope | a gate that cannot be run twice is not a gate | hand-reset between runs |

## 12. Open questions

1. **Can `tig-server` hold six concurrent Claude agents?** 3.7 GB across six
   agents plus a herdr server plus `make check` is not obviously enough. M3's
   slice runs one agent at a time and will not answer it; the first multi-agent
   tick will. If the answer is no, the lever is `slots`, and parent §8's claim
   that the disjoint-area count is the real ceiling gets tested from the other
   direction.
2. **What is the right `poll_seconds`?** Parent §8 writes 60. A tick that fetches
   comments for every live task is more `gh` calls than M2's single list, and
   secondary rate limits are a real thing. Instrument before tuning.
3. **Does `agent_status: done` mean the agent finished, or that its pane died?**
   The mapping from herdr's five statuses to `AgentState`'s three is M3's to
   make, and `dirty` has no herdr equivalent at all — it is derived from the
   absence of a valid packet. Whether that derivation is right is the kind of
   thing only a real dirty exit answers.
4. **Does the price table drift?** It is a second place Anthropic's prices are
   written down. Nothing in the repo can notice when it is wrong, and the
   symptom — a ceiling that binds too early or too late — is not obviously a
   pricing symptom.
