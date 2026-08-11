# Inputs to M2, carried out of M0–M1.5

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

## 3. Fixture validity — what the final review found

The fixture grades some things it does not test. Fixed already: issue 6 (was
trivially solvable and mis-scoped) and rubric 01 (graded a weaker property than
its issue demanded). Still open:

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
- **`_post` can leave an issue with no state label.** It removes all eight then
  adds one; a failure between the two strands the task invisible to any label
  query. `gh issue edit --add-label X --remove-label Y` is one call.
- **No dirty-worktree check before a handoff.** An implementer that forgets to
  commit hands the reviewer a `head_sha` that does not contain the work, and both
  packet and verdict validate.
- **The fleet suite depends on a hardcoded absolute path** into the fixture repo
  (`Path.home() / "TIG/fleet-fixture/.agents/schemas"`, in three test modules),
  and validates against that repo's *working tree*. Vendor a copy or skip when
  absent.
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
