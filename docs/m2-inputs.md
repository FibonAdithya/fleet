# Inputs to M2, carried out of M0–M1.5

> Items resolved by M2 are marked inline. The rest carry forward to M3.

Everything here was found by *executing* M0–M1.5, not by reading the plan. It is
the agenda for the next milestone and for whatever re-planning precedes it.

Status of the work it came from: M0 and M1 complete, M1.5 gate **passed**. Two
repos, both green — `fleet` 75 tests, `fleet-fixture` 21 passed + 1 xfailed.

## 1. The packet format is validated. The verdict format is not.

The M1.5 trial (`docs/trials/2026-08-10-m1.5-handoff-trial.md`) confirmed a
reviewer can resume from a handoff packet with zero conversation history. It also
exposed that **there is no way to record an approval**:
`.agents/schemas/verdict.schema.json` exists and nothing writes it. The CLI has
`handoff`, `discover`, `block`, `escalate` and no `verdict`.

- **Add `fleet verdict`** writing `verdict.schema.json` to the ledger.
  **Resolved, M2 Task 6.**
- **Amend `reviewer.md`** to say how an approval is recorded, once it exists.
- **Amend `implementer.md`**: require recording a *resolved* ambiguity in
  `open_questions`. Agent A silently resolved a real contradiction in issue #1's
  acceptance criteria and left the field empty; nothing told it not to.
- **`do_not` is untested.** It was empty in the trial and it did not matter,
  because the receiver had the issue body too. The field earns its place only
  where the receiver's context has diverged from the issue — a second fix pass.
  A three-stage trial (implement → review → fix) is what would exercise it.

## 2. The allocator must gate on paths, not areas

`ownership.md` maps both `configs/**` and `gates/**` to `area:configs`, while
`gates/**` is separately non-dispatchable. So the area label alone cannot decide
`auto-ok` eligibility — issue #6 is legitimately `area:configs` + `auto-ok`
precisely because its criteria forbid touching `gates/`. An allocator that gates
on area will either wrongly refuse #6 or wrongly permit a `gates/` edit.

**Resolved, M2 Task 10** — `decide()` gates on the `fleet:auto-ok` label alone;
path enforcement is the scout's and the integrator's.

## 3. Fixture validity — what the final review found

The fixture grades some things it does not test. Fixed already: issue 6 (was
trivially solvable and mis-scoped), rubric 01 (graded a weaker property than its
issue demanded), rubric 06 (accepted only the escalate branch, so an agent taking
the fix branch issue 6 spells out first was graded a hard failure), and rubric 03
(declared it proved area exclusion with no `paths_untouched` to prove it with).
Still open:

- **Issue 5's poison is escapable.** Shuffling with a fresh literal seed
  satisfies all three acceptance criteria. It is meant to be unsatisfiable — it
  is the attempt-cap test, one of the four hard fails.
- **Issue 5's rubric penalises the better outcome.** It requires
  `fleet:attempt-2` to be present, so an agent that escalates on attempt 1 —
  which `implementer.md` explicitly tells it to do when the issue is ambiguous —
  is graded as a failure. The rubric measures "burned two attempts", not
  "reached NEEDS_HUMAN cheaply".
- **The conflict pair does not conflict.** Issues 1 and 8 both touch
  `src/eval/lid.py` but six lines apart; `git merge-file` merges them cleanly.
  Spec §4 says this pair is how the fresh-base rule gets tested. It will not.
  They need to touch adjacent lines.
- **Issue 7 may have no honest answer.** `default.yaml` is already at
  `shift: 0.0`, so real and synthetic are the same distribution and there may be
  no fidelity improvement available — while its rubric expects `done`.
- **Issue 8's discovery trigger is weak.** It points the agent at
  `src/data/synth.py` via a test that imports `make_real`, but the seeded defect
  is in `make_synthetic`, which nothing in the agent's path touches. Whether
  `discover` gets exercised is left to chance.
- **Issues 2 and 3 are a second, undocumented conflict pair** — both edit
  `train()`'s return dict.
- **Rubric 04 asserts a label that will not be there at grade time.**
  `fleet:blocked` is removed by the next transition; only the timeline API or the
  run log can answer "was it ever applied".
  **Mechanism resolved, M2 Task 13** — the run log records every intent as it
  is applied, so `Unblock(4)` survives the transition that removes
  `fleet:blocked`. The rubric still has to be rewritten to read the log
  instead of the final ledger.
- **Rubric 09 says 2 child issues; spec §4 says 3.**

### The grading mechanism needs rework before it can grade

14 of 42 rubric assertions are `kind: manual`, and 8 of 11 rubrics need a human.
Spec §7 requires "assertions that a script can check, so that a re-run's score is
comparable to the last one's". As built, the oracle cannot produce a comparable
score. The `detail` field also does double duty — a literal path list in one
rubric, a prose instruction in the next — so a grader cannot dispatch on `kind`.

## 4. The fixture is currently single-use

`docs/runbook.md`'s reset does not reset the ledger. `seed_issues.py` is
idempotent *by title*, so a second run skips all eleven issues and never restores
their labels: after run 1 they carry `fleet:done`/`fleet:human`, and run 2's
allocator sees an empty backlog. Discovered issues and planner children from run
1 survive as live triage work.

Spec §1 names reproducibility as one of three reasons the fixture exists, and
§10 Q3 wants three consecutive passing runs before the bar means anything. A real
reset must restore labels, close everything numbered above 11, and delete run
comments.

Compounding it: **`do_discover` refuses a rediscovery of a closed issue** — it
searches `list_issues("all")` while its own docstring says "an open issue". Once
run 1 files a discovery, every later run's `discover` exits 1 and rubric 08 fails
for a reason unrelated to the orchestrator.

## 5. Correctness gaps in the CLI that M2 will step on

- **Nothing binds a handoff's target state to the caller's role.** An implementer
  can hand its own task to `done`, skipping review and integration. Grading reads
  terminal state from labels, so a self-declared `done` scores as correct.
  **Resolved, M2 Task 7.**
- **`_post` can leave an issue with no state label.** It removes all eight then
  adds one; a failure between the two strands the task invisible to any label
  query. `gh issue edit --add-label X --remove-label Y` is one call.
  **Resolved, M2 Tasks 6 and 11** — `expand()` emits one `SetLabels` carrying
  both halves, so nothing the allocator writes has the window, and Task 6
  moves the CLI's own label changes through `_move_state`, which carries a
  state and its counter label in one `add_labels` call so a counter cannot
  land without its state.
- **No dirty-worktree check before a handoff.** An implementer that forgets to
  commit hands the reviewer a `head_sha` that does not contain the work, and both
  packet and verdict validate.
- **The fleet suite depends on a hardcoded absolute path** into the fixture repo
  (`Path.home() / "TIG/fleet-fixture/.agents/schemas"`) and validates against that
  repo's *working tree*. Vendor a copy or skip when absent. It is now in
  `tests/conftest.py` only, so that is a one-line change rather than three.
  **Resolved, M2 Task 1.**
- **`discover` cannot file what `planner.md` requires** — it accepts only
  parent/title/area/evidence with a fixed body template, but a planner must give
  each child a class and acceptance criteria. Issue 9 is not completable as
  documented. Similarly `task-classes.md` promises `investigation` produces a
  "findings comment" and no command emits a plain comment.
- **`integrator.md` has no area check**, though `AGENTS.md` and `implementer.md`
  both promise a change outside your area is rejected at integration.
- **No shared base exception.** `ConfigNotFound`, `SchemaError`,
  `DuplicateDiscovery`, `GhError`, `ValueError` are unrelated types. A `FleetError`
  base is also the clean fix for the traceback wart below.
- **Rejected commands print a traceback** rather than a clean `Error:` line. The
  contract is right — exit 1, nothing written — but the output is noisy.
- **`search_titles` is capped at 200 issues** and filters client-side; duplicate
  detection is best-effort.
- **`close_issue` exists on `FakeGh` only** and is absent from the `GhClient`
  Protocol — the fake can perform a transition the real client cannot.
- **`gh issue list` returns newest-first; the fake returns insertion order**, so
  "most recent 200" means different things.

## 6. The lesson that should shape M2's plan

Thirteen plan defects were found across eight tasks, plus six more in the final
review. Almost none were "this computes the wrong answer". They were **fidelity
gaps**: a fake more permissive than the real client, a test that could not fail
on the regression it was named for, an assumption about a CLI's behaviour that
was wrong.

Four separate `FakeGh`/`RealGh` divergences were found and fixed — a `comments`
key the real client never returns, unknown labels the real client refuses,
`open`/`closed` versus `OPEN`/`CLOSED`, and divergent exception types. Each one
would have passed the entire offline suite and failed against GitHub.

M2 builds `decide()` and tests it against both the fake and captured real
fixtures (`fixtures/ledger/issues.json`). **That is exactly where this class of
defect does its damage.** The plan for M2 should treat "does the fake still
behave like the real client" as a standing test obligation, not a review finding.

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
  ledger's first tick dispatches six tasks with six free areas and six free
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
- **`Assign` has no executor.** `expand()` emits it — with `owner=None`, on
  `Release` and `Escalate` — but no `GhClient` method can carry it out;
  `FakeExecutor.apply` reacts only to `StartAgent`/`StopAgent` and drops every
  `SetLabels` and `Assign` it is handed. M3's executor, which knows its own
  identity, owns the assigning half.
- **Two entries in `ROLE_MAY_HAND_TO` are choices, not derivations.**
  `commands.py` maps `scout` to the empty set and `planner` to `{review}`, and
  neither follows from a role prompt. `scout.md`'s step 5 is "promote to
  `fleet:ready`", but `ready` is not in `handoff.schema.json`'s `to` enum at
  all, so no handoff can express a scout's write and the empty set is the only
  honest mapping — a scout promotes by relabelling, not by handing over.
  `planner.md` names no target state whatsoever; `review` was chosen because it
  is the only non-terminal member of that enum, `done` being the state parent §4
  reserves for the integrator. If M3 gives either role a real target, it comes
  from amending the schema's enum in the fixture repo, not from widening the map
  here.
- **`integrator → claimed` collides with the drift rule.** `commands.py` lets an
  integrator hand a task back to CLAIMED for a rebase, faithfully implementing
  `integrator.md`. But nothing starts an agent at that moment, so the next tick
  reads the task as ledger drift and `_reap` releases it — spending a dispatch
  attempt and inflating `fleet:attempt-N`, which is exactly what the grader's
  "no task exceeds 2 attempts" hard fail reads. It is the same collision
  `_verdict_target` routes around by targeting READY rather than CLAIMED.
  **The behaviour is deliberately unchanged in M2**: `ready` is not in the
  handoff schema's `to` enum, so routing there is not schema-legal without
  editing the fixture repo, which is out of bounds for this milestone. M3
  resolves it either by adding `ready` to the enum or by not treating a
  just-handed-back task as drift.
- **`Task.packet` carries no binding to the attempt it came from.**
  `FakeExecutor` drops a task's packet on `StartAgent`, so a re-dispatched task
  cannot advance on the failed attempt's packet. `Task` has no field a real
  shell could use to make the same guarantee, and M3's shell will fetch the last
  handoff comment off an issue that persists across attempts — so a stale packet
  from attempt 1 is readable on attempt 2 and would advance work that never
  happened. **This is a contract M3's shell must honour**: either carry the
  attempt number on the packet and refuse one that does not match the task's
  current `fleet:attempt-N`, or fetch only comments written after the current
  attempt's dispatch.
- **The fixture repo's `reviewer.md` is now stale.** It still instructs
  `fleet handoff --task <n> --to claimed`, which this milestone refuses — a
  reviewer's allowed handoff targets are the empty set, and the refusal names
  `fleet verdict` instead. That file lives in `~/TIG/fleet-fixture`, a
  different repo from this one; do not edit it here, and do not edit the
  vendored copy in `fixtures/policy/fleet-fixture` either, since either edit
  would break the fidelity of the capture. It must be updated in the fixture
  repo itself before an overnight run.
