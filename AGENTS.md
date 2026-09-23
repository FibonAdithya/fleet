# Agent guide

Read this first, then follow the links. This file is a router: it says which
document is authoritative, what must not be broken, and what needs a human.
It restates as little as possible, because a second copy of a fact is a copy
that goes stale.

## What this project is

`fleet` runs several coding agents at once against one target repository,
unattended, and moves work between them through schema-checked handoff packets
posted as GitHub issue comments. The target repository's issues are the ledger,
its own `fleet.toml` and policy files say who may do what, a single daemon is the
only writer of ownership, and every agent writes to the ledger through the
`fleet` CLI and never through `gh` directly. Success is a morning with work
merged into the integration branch and `make check` green, a few tasks stopped
at a specific question for a human, and no task that burned the night re-failing
the same way.

It is not an issue tracker, not a replacement for herdr (which owns worktrees,
panes and agent processes), and not a merge authority: promotion to `main` stays
human. This repository is the tool; the repository it operates is a separate
checkout, and the tests run against a vendored copy of one under
`fixtures/policy/fleet-fixture/`.

## Source of truth, in order

When two documents disagree, the one higher in this list wins.

1. **The code** and its configuration. If a document describes behaviour the
   code does not have, the document is wrong.
2. **The vendored data contract**, `fixtures/policy/fleet-fixture/`: the
   handoff and verdict schemas under
   `fixtures/policy/fleet-fixture/.agents/schemas/` and the example
   `fixtures/policy/fleet-fixture/fleet.toml` are a captured copy of the target
   repository's policy layer. The label vocabulary the ledger is made of is
   `src/fleet/labels.py::STATES` and its neighbours.
3. **`docs/runbook.md`** — operator procedures, each one exercised live against
   the fixture repository. The commands you run to seed, plan, run, watch and
   reset a fleet.
4. **`README.md`** — setup and the gate.
5. **`docs/ai/`** — *not authoritative*. Design specs and plans written by
   agents during development, kept for the reasoning behind decisions. They
   are not updated as the code changes. See `docs/ai/README.md`. Agents
   writing a new design spec put it in `docs/ai/specs/`; implementation
   plans go in `docs/ai/plans/`; nowhere else.

## Invariants

These are silent until violated. Nothing in the test suite catches them, and
each is easy to break while believing you are making progress.

1. **Only the daemon writes ownership.** Agents never self-assign and the CLI
   has no command that sets an assignee or applies `fleet:claimed`. One writer
   is what makes two agents unable to claim the same task without any lock;
   adding a way for an agent to claim, or to apply `fleet:auto-ok` to its own
   task, reopens the race the design removed. The rule is in the design spec's
   section 5 and the allocator is `src/fleet/decide.py::decide`.

2. **The role-to-target table is closed, and a verdict is REVIEW's only exit.**
   `src/fleet/commands.py::ROLE_MAY_HAND_TO` says which states each role may
   hand to; `src/fleet/commands.py::VERDICT_ROLES` says who may write a verdict.
   An implementer that could reach `done` would skip review and integration,
   and grading reads terminal state from labels, so a self-declared `done`
   scores as correct. Widening either table is a privilege grant, not a
   convenience. An unknown or unset `FLEET_ROLE` is refused for the same reason.

3. **Policy parsing is strict; a permissive path is a defect.** A silently
   empty ownership map disables area exclusion and the fleet runs six agents
   into one area; a role missing from the target repo's `Role | Agent | Model`
   table used to pass startup and silently disable the declared-versus-observed
   model check. `src/fleet/policy.py::load` refuses both at load time, on
   `fleet plan` as well as `fleet run`. Do not make it lenient to get a fixture
   to load; fix the fixture. See
   `docs/runbook.md#declaring-agents-and-models`.

4. **Fakes may not be more permissive than the real clients.** Four of the
   first milestone's defects were cases where `FakeGh` or `FakeHerdr` accepted
   what GitHub or herdr would not, and every one passed the whole offline
   suite. Parity is checked against captured real output under
   `fixtures/ledger/`, `fixtures/herdr/` and `fixtures/cost/`, and the policy
   layer is a vendored copy, never a live path into another repository's
   working tree. When you change a fake, change the real client the same way,
   and re-capture fixtures deliberately, saying so in the commit. The lesson
   is written up in `docs/m2-inputs.md#6-the-lesson-that-should-shape-m2s-plan`;
   each fixture directory's `README.md` records how it was captured and what
   it does not show.

5. **A cost the pricer cannot account for stops the fleet; it is never
   priced at zero.** `src/fleet/cost.py::UnpricedTranscript` and
   `src/fleet/cost.py::UnpricedModel` are collected by the tick and arm a
   fleet-wide drain: in-flight agents finish, nothing new starts. That refusal
   is the budget ceiling working. Do not add a price row to silence one
   (Claude Code's `<synthetic>` pseudo-model in particular carries zero tokens
   and is already ignored), and do not catch the exception and continue.
   `docs/runbook.md#the-codex-gate` says which transcript shapes refuse and
   why a codex agent that spawns threads drains the run.

6. **The daemon holds no authoritative state, and the core has no clock.**
   Task state lives in labels, ownership in the assignee, and the night's
   spend is rebuilt from the run log by run id, so `fleet run` resumes by
   default and a restart loses only idle timers. `decide`, `expand`, `cost`
   and `simulate` take `now` as an argument and do no I/O; a test that sleeps
   to synchronise, or a `datetime.now()` inside the core, would pass today and
   flake on a loaded box. See `src/fleet/daemon.py::Daemon` and
   `docs/runbook.md#running-the-daemon`.

7. **`date-time` format checking exists only because `rfc3339-validator` is
   installed.** `jsonschema` skips the format silently when it is absent, so
   dropping that dependency, or letting a resolution lose it, leaves the
   handoff schema's `from.run` unenforced with every test still green. This is
   why CI installs with `uv sync --locked` and why the dependency is commented
   in `pyproject.toml`. Validation is `src/fleet/schemas.py::validate`.

## What "done" means

Run from the repo root:

    make check

That is the same command CI runs (`.github/workflows/ci.yml`). No target uses
`|| true`; a red suite is a failure, not a warning. The `Makefile` is the
executable definition of a valid change. A pull request also gets a model
review (`.github/workflows/review.yml`) that can comment and never push; it is
advisory, and `.github/workflows/ci.yml` is the only required check.

## What requires a human

Do not decide these yourself. Raise them and stop.

- Changing the label vocabulary in `src/fleet/labels.py` or the state machine
  it encodes. Labels are repository state on GitHub; a renamed state label is
  one `gh` has never heard of, on every issue that carries the old one.
- Changing the handoff or verdict packet schema, or the version ids in
  `src/fleet/packet.py::SCHEMA_IDS`. Real issues carry comments in the current
  shape and the reader must keep parsing them.
- Widening `ROLE_MAY_HAND_TO` or `VERDICT_ROLES` (invariant 2).
- Changing a default budget, attempt cap, fix-pass cap, rebase cap or the drain
  fraction (`src/fleet/decide.py::_DRAIN_FRACTION`). These bound what an
  unattended night can spend or repeat.
- Adding a `[prices]` row to make a refusal go away (invariant 5).
- Re-capturing any fixture under `fixtures/`, and raising the `agentify` pin or
  `requires-python` in `pyproject.toml`.
- Any diff touching `Makefile`, `.github/workflows/`, or deleting tests net.
  This is the guard the design asks the integrator to apply to agents; it
  applies to this repository too.
- Merging to `main`. Promotion is human by design.

To report a bug in this project, file an issue with the `agent-reported`
label:

    gh issue create --label agent-reported --title "..." --body "..."

The label is what routes the issue to a person. An issue without it notifies
nobody.

## Where to look

| Task | Start here |
|---|---|
| Set up and run day-to-day commands | `README.md` |
| Run the gate | `Makefile` |
| Set up a development environment | `make setup` (a target in `Makefile`) |
| Operate a fleet: seed, plan, run, watch, reset | `docs/runbook.md` |
| What the allocator does on a tick (dispatch, reap, unblock, drain) | `src/fleet/decide.py::decide` |
| How an intent becomes label and comment effects | `src/fleet/expand.py::expand` |
| The CLI write path and who may hand where | `src/fleet/commands.py::ROLE_MAY_HAND_TO` |
| The label vocabulary | `src/fleet/labels.py::STATES` |
| Reading the target repo's `fleet.toml` and policy files | `src/fleet/config.py::load_config`, `src/fleet/policy.py::load` |
| An example target-repo policy layer | `fixtures/policy/fleet-fixture/` |
| Pricing a transcript, per provider | `src/fleet/cost.py::ClaudeProvider`, `src/fleet/cost.py::CodexProvider` |
| Talking to herdr, and what its output looks like | `src/fleet/herdr.py::RealHerdr`, `fixtures/herdr/README.md` |
| The daemon loop, run log and state socket | `src/fleet/daemon.py::Daemon`, `src/fleet/runlog.py`, `src/fleet/statesocket.py` |
| Replaying a night offline | `fixtures/scenarios/`, `src/fleet/simulate.py::run`, `tests/test_scenarios.py` |
| Captured real fixtures and their caveats | `fixtures/ledger/README.md`, `fixtures/cost/README.md`, `fixtures/policy/README.md` |
| What the last milestone left for the next (records, not rules; resolved items are marked inline) | `docs/m2-inputs.md`, `docs/m3-inputs.md` |
| Why a decision was made (non-authoritative) | `docs/ai/specs/` |
