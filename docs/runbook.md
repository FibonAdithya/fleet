# Runbook

## Seeding or resetting the fixture backlog

`FibonAdithya/fleet-fixture` is the owner's instance. A reader works on their own
fork (see [the README](../README.md#trying-it-on-the-demo-target)): run
`export FLEET_FIXTURE_REPO=<you>/fleet-fixture` in your shell for the seed and
reset scripts, set
`FLEET_FIXTURE_CLONE` for `fixtures/seed/reset.sh`'s clone path (default
`$HOME/TIG/fleet-fixture`), set `[repo] slug` in the fork's `fleet.toml`, and run
`gh repo set-default <you>/fleet-fixture` in the fork's checkout. A
`gh repo fork --clone` checkout defaults to the parent repository, and the reset
commands below pass no `--repo`. Paths like `~/TIG/fleet` and
`~/TIG/fleet-fixture` in the commands below are the owner's checkout locations;
substitute yours.

    cd ~/TIG/fleet && FLEET_FIXTURE_REPO=<you>/fleet-fixture ./fixtures/seed/seed_issues.sh

Creates the full label vocabulary first, then the eleven issues. Idempotent both
ways — an existing label is left alone (colour included) and an existing title is
skipped — so this also stands up a fresh repo from nothing. Point it elsewhere
with `FLEET_FIXTURE_REPO=owner/name`.

Labels have to come first: `gh issue create --label X` does not create `X`, it
fails and creates no issue at all.

## Agent environment

Every agent process must have these set, or packets will be attributed to
`manual`:

    FLEET_ROLE=<implementer|reviewer|invariant-reviewer|planner|integrator|scout>
    FLEET_AGENT=<stable id, e.g. fleet-3>

The CLI warns on stderr when `FLEET_AGENT` is unset *or empty* — `export
FLEET_AGENT="$AGENT_ID"` with `AGENT_ID` unset exports an empty string, which is
a dispatch bug and not a value. `manual` is a legitimate value for a human at a
keyboard, which is exactly why an unattended run that silently produced it would
be indistinguishable from that bug.

`FLEET_ROLE` is no longer only attribution: a handoff's target state is checked
against it. An implementer may hand to `review` and nothing else; only an
integrator may reach `done`; a reviewer leaves REVIEW by writing a verdict and
cannot hand out of it at all. An unrecognised role is refused rather than
permitted, so a typo is an error and not a privilege escalation — and so is an
*unset* `FLEET_ROLE`, which used to fall back to `implementer` silently and so
handed out exactly the privilege this check exists to fence off.

## The five write commands

    fleet handoff  --task N --to review --did "..." --checks "..." --command "make check"
    fleet discover --from N --title "..." --area data --evidence "path:line ..."
    fleet block    --task N --on M --did "..."
    fleet escalate --task N --question "..." --considered "..." --did "..."
    fleet verdict  --task N --verdict approved --head-sha SHA --checks-rerun "make check: pass"

`block` resolves itself when task M lands. `escalate` wakes a human and is
terminal. Choosing the wrong one is the difference between a task that fixes
itself at 3am and a task that sits in the morning queue.

`handoff` cannot reach `blocked` or `human` — it refuses, naming the right
command. A handoff to those states would carry no `blocked_on` and no
`question`, leaving a task nothing could ever resolve. The converse is refused
too: `blocked_on` cannot ride along on a packet bound anywhere else, and a task
cannot block on itself.

`handoff` requires a real branch and refuses on a detached HEAD, because the
receiver has to check something out. `block` and `escalate` do not — they are the
only paths that reach a human, and a detached HEAD mid-bisect is when an agent
most needs them. Their packets record `branch: "detached at <sha>"`.

## Reading a task's history

    gh issue view N --json comments --jq '.comments[].body'

## Verified live against `FibonAdithya/fleet-fixture`

The following were exercised end to end against real GitHub, then cleaned up:

| Behaviour | Result |
|---|---|
| `handoff` posts a schema-valid packet | `fleet:claimed` → `fleet:review`, `area:` preserved, real SHAs from git |
| Invalid escalation (blank question) | exit 1, **zero** writes — comment count unchanged |
| Schema-invalid handoff (empty `did`) | exit 1, **zero** writes |
| `discover` files to triage | new issue with `fleet:triage`, `area:`, `discovered-by:N`; no `fleet:ready`, no `fleet:auto-ok` |
| `discover` creates its parentage label | `discovered-by:12` did not exist and was created on demand |
| Duplicate discovery | refused; issue count unchanged |
| Parent task during discovery | labels and comments untouched |

## Seeing what the allocator would do

    cd ~/TIG/fleet-fixture && fleet plan

There is no `--dry-run`/`--no-dry-run` flag any more — `fleet plan --help`
takes no options at all beyond `--help` itself. `fleet plan` only ever
previews and never writes; that used to be a mode you opted into, now it is
the only mode there is.

It reads every row through `reader.read_ledger`, the same ledger read the
daemon itself does on every tick — not `gh.list_issues("open")`, which used to
give a systematically different answer: `Task.blocked_on` and a rebase
handback both come from a handoff packet an open-only query never fetches, and
that query also silently dropped the closed dependency that satisfies an
UNBLOCK. Now the preview is the same answer the next real tick would give.

Runs one tick of `decide()`, prints the intents, and writes nothing. The
client is wrapped so that every method outside a read allowlist raises — deny
by default, so a mutating method added later is refused until someone
classifies it. The guarantee is a property of the code path, not of having
read it carefully — and it is unconditional now. There used to be a
`--no-dry-run` that swapped in an unwrapped, writing client for a body that
made no write either way, so the flag was accepted and did nothing; it was
removed along with `--dry-run` rather than kept as a no-op.

The tick runs with **no live agents**, because `fleet plan` always calls
`decide()` with an empty agent list. When the ledger holds a `fleet:claimed`
task the output says so above the intents: with no agents that task is
indistinguishable from ledger drift, so the `Release(N, no_agent, 2)` printed
for it is an artefact of the preview rather than a recommendation. Against an
idle backlog the output is exactly the dispatches the allocator would make.

## Running the daemon

    cd ~/TIG/fleet-fixture && fleet run

Starts the allocator loop: read the ledger, decide, log, apply, render — repeat
every `poll_seconds` (default 60, `[daemon] poll_seconds` in `fleet.toml`) until
`SIGINT` or `SIGTERM`. Both signals get the same clean shutdown a Ctrl-C gives —
the tick in flight finishes, the socket file is removed — because exiting
mid-tick would leave a task labelled `fleet:claimed` with no agent, which the
next run's REAP only clears at the cost of an attempt.

Watch it from another terminal:

    watch -n5 fleet status

`fleet status` is a client of the daemon's Unix-domain socket
(`[daemon] socket`, default `<repo>/.fleet/fleet.sock`): each call connects,
reads one rendered snapshot of the last completed tick, and disconnects. If
nothing is listening it says so and exits non-zero — distinct from an empty
table, which would read as an idle fleet rather than a dead one. If the daemon
has started but not yet finished its first tick, `fleet status` says "no tick
yet" instead, which is a real and different state from "no daemon".

`--once` runs a single tick, prints the same rendering `fleet status` would
show, and exits — for a supervised slice, or for checking a fresh `fleet.toml`
works before leaving the daemon running unattended overnight:

    fleet run --once

The run log (`[daemon] runlog`, default `<repo>/.fleet/run.jsonl`) is the
night's durable memory: every intent the daemon decided on, with the cost it
finalised, one JSON line per entry. A restart rebuilds `spent_usd` for the
night by summing this file's entries for the same run id — the only state a
restart loses is the idle timers, so a stall in progress is noticed up to
`stall_minutes` late rather than forgotten.

**What counts as a stall.** An agent is released as `stalled` when, for
`stall_minutes`, neither herdr's `state_change_seq` nor the combined size of its
transcript and its subagents' transcripts has changed. The sequence moves only
on a status change, so on its own it held still through one long turn; the
transcript grows with every tool call and result. The remaining blind spot is a
single tool call that writes nothing for longer than `stall_minutes`, such as a
long test suite, so set `stall_minutes` above the longest such call the target
repository runs. herdr's `revision` is not used: measured on herdr 0.7.5 it
moved while an agent sat blocked in one tool call, so a hung agent would never
read as stalled.

**`fleet run` resumes by default.** With no `--run-id`/`--new-run`, it takes
the run log's last line as tonight's run id and rebuilds `spent_usd` from
every entry under that id, rather than minting a fresh id and starting
`spent_usd` back at zero. That is deliberate: the restart is half of the M3
gate, and a fresh id every start would recover none of the night's spend on a
2am restart, handing the fleet a second full budget. The first line the
daemon prints says which of the three happened, in one place:

    fleet run: run_id=<id> (resumed from the run log) spent=$<amount> socket=<path>

— `(resumed from the run log)`, `(given on the command line)` for an explicit
`--run-id NAME`, or `(new run)` — plus what this process believes that night
has already spent. A daemon resuming at `$0.00` against a night that already
spent $20 looks exactly like a daemon starting a fresh night; that line is the
only way to tell them apart. To start a genuinely new night rather than
rejoining the last one, pass `--new-run`. Without it, `fleet run` after a
reset resumes whatever run id the log last held and can drain immediately
against that night's old spend — loud (the startup line says so, and a
drained night dispatches nothing), but real: see "Resetting after a graded
run" below.

Worktrees for live agents are created under `[daemon] worktrees` (default
`~/.fleet/worktrees`, outside the repo on purpose — a scratch worktree under
the repo root would show up as untracked in `git status`).

`fleet:rebase-N` is a third per-task counter alongside `fleet:attempt-N` and
`fleet:fix-pass-N`: an integrator's rebase attempts are capped by
`max_rebases` exactly as an implementer's attempts are capped by
`max_attempts`, and a reviewer's fix passes by `max_fix_passes`.

## Declaring agents and models

Two config surfaces are required since the multi-provider branch, and both
now fail loudly at startup — `policy.load`, which runs on `fleet plan` as
well as `fleet run` — rather than passing quietly and failing mid-run:

- **`fleet.toml`**: one `[agents.<kind>]` table per agent CLI kind any role
  is routed to, naming `model_flag` (how that CLI spells its model flag —
  `--model` for `claude`, `-m`/`--model` for `codex`) and, optionally,
  `args` (fixed launch flags, e.g. `--permission-mode acceptEdits` for
  `claude`, `--full-auto` for `codex`). A role routed to a kind with no
  matching `[agents.<kind>]` table used to pass startup and then fail
  identically at every dispatch of that role — `HerdrExecutor._start` raises,
  `_begin` swallows it per task, and the whole backlog walks to NEEDS_HUMAN
  overnight having launched nothing.
- **The target repo's `task-classes.md`**: a `| Role | Agent | Model |`
  table naming, for every role any task class routes to, which CLI it runs
  on and which model. A role missing from this table used to pass startup
  too, and is worse than a missing `[agents.<kind>]`: besides failing at
  dispatch the same way, `Policy.agent_for(role)` returns `None` for it,
  which silently disables the declared-vs-observed model mismatch check for
  that role.

Get either wrong and `fleet plan` or `fleet run` refuses before dispatching
anything, naming the role and the missing table or config entry.

**What the gate still cannot ask for.** It checks the models a policy
*declares*, so a model that only ever appears inside a transcript is outside
its reach. Claude Code's `<synthetic>` pseudo-model is the one that matters in
practice: it appears in 38 of the 199 local transcripts that carry usage at
all (19%), it is never anything a `Role | Agent | Model` table names, and its
usage block is all zeros in every one of the 72 records of it found there.
`cost._claude_cost` therefore prices no record that carried zero tokens and
demands no rate for one -- zero tokens cost $0 at every rate, so a refusal
there reported nothing an operator could act on while discarding the real cost
already read from the same transcript and arming the fleet-wide Drain. Do NOT
add a `[prices."<synthetic>"]` row to work around a refusal; if one appears,
the record carried real tokens and the rate is genuinely missing.

**Subagent spend is counted on the Claude path.** `cost.transcript_path`
globs `~/.claude/projects/*/<uuid>.jsonl` — one level — and Claude Code
writes subagent transcripts one level deeper, at
`<project>/<uuid>/subagents/agent-<child>.jsonl`. Measured over 1,204 local
transcripts: 214 at the level the glob searched and 990 (82%) below it, and
across the 66 sessions holding both halves, **46.7% of all tokens sat in the
half nothing read** (worst ratios 3.9x, 3.0x, 2.7x subagent-to-parent). That
spend was never *refused* — the parent priced fine, so nothing reached
`unpriced`, no Drain armed, and both `per_task_usd` and `fleet_usd` were
simply short by roughly half on any task that delegated.

`ClaudeProvider.session()` now returns the parent plus every child beside it
and `parse_snapshot` sums the fold across all of them. Two things worth
knowing at 2am:

* The **observed model** still comes from the parent alone. 43 of those 66
  sessions run a model in a child the parent never ran — opus parents
  delegating to sonnet and haiku — so unioning the sets would hand
  `MULTIPLE_MODELS` to `decide._model_mismatch` and escalate two thirds of
  every task that spawns a subagent. Delegation is not substitution; the
  agent a policy declares is the parent.
* A child naming a model with **no `[prices]` row refuses the whole session**,
  exactly as its parent would. So a role whose subagents run a model the
  policy never declared now drains where it previously under-billed in
  silence. Add the rate rather than removing the subagent.

## The codex gate

Before routing any role to `codex` in a graded run, launch one codex agent
through herdr and confirm its transcript emits component breakdowns
(`input_tokens` / `output_tokens` populated in `event_msg.payload.info
.total_token_usage`), not just a cumulative `total_tokens` with every
component zero. `CodexProvider.read` refuses the latter shape rather than
pricing it at $0 — correct, but it means a codex agent stuck on that shape
never gets a `Reading` its cost can be checked against, and the run drains
on the first tick that tries to account for it. That refusal is loud (an
`UnpricedTranscript` on the first tick, not a slow leak), but check before
the run rather than during it: fixtures/cost/codex-total-only.jsonl and
fixtures/cost/codex-breakdown.jsonl are real captured examples of both
shapes, and locally 50 of 80 sessions were total-only against 30 with
breakdowns, on the same CLI version and the same day — so neither the date
nor `codex --version` tells you which one a given launch will produce.

**A codex agent that spawns subagent threads drains the fleet.** codex writes
each spawned thread to its own rollout file, named by the *child* thread's id,
not by the session id herdr reports for the parent. Measured across 81 local
rollouts: 64 are top-level (filename uuid == `payload.session_id`), 17 are
subagent threads (filename uuid is the child id; `session_id` inside the file
still names the parent). On the one local session with children, the parent's
own final `total_token_usage.total_tokens` was 13,435,746 while its 14
children totalled **37,366,303** — 74% of that session's real spend, in files
`CodexProvider.session()` cannot resolve from the parent's session id.

Issue #5 closed the silence, not the gap. `CodexProvider.read` now raises
`UnpricedTranscript` the moment the parent's own lines show a live subagent
roster (`world_state.payload.state.environments.subagents`, a list of
*nicknames* — enough to prove children exist, useless for finding their
files), so the tick collects it into `unpriced` and `Daemon.tick` drains:
in-flight agents finish, nothing new starts. That is a loud stop on the first
tick, not a slow leak, but it *is* a stop. Before routing a role to codex,
establish whether its prompt leads it to spawn threads. If it does, that
agent's task cannot complete under a budget ceiling until codex child
discovery is built — the parent's spend alone was 26% of the real total on
the one session measured, and a confident number for a quarter of a session
is worse inside a ceiling than no number at all.

The guard keys on the *field* being non-empty, not on the record existing: a
single local rollout carries 35 `world_state` records, and refusing on the
type alone would drain every codex agent on the box.

**The codex session id herdr reports is environment state, not repo state.**
It reaches herdr via `~/.codex/herdr-agent-state.sh`, a `SessionStart` hook on
the run box that calls `pane.report_agent_session` — nothing in this repo can
confirm it is installed. If it is missing, every codex agent's transcript is
unresolvable, every codex agent reads as free with an empty `unpriced`
collector, and the drain this whole design relies on never arms. Confirm the
hook is present before a graded run, the same way the breakdown shape above
is confirmed.

**One more asymmetry worth knowing at 2am:** a codex transcript naming two
models raises `UnpricedTranscript` and drains the whole fleet, while the same
situation on the Claude path records `MULTIPLE_MODELS` and escalates just
that one task. Both are correct — codex's cumulative totals genuinely cannot
be split between two rates, where Claude Code's per-message totals can — but
a codex model substitution costs the whole night's remaining dispatches where
the equivalent Claude one costs a single task.

## Running the scenarios

    uv run pytest tests/test_scenarios.py -v

`fixtures/scenarios/` holds one file per claim, and each names its claim in its
`proves` field. Five of the ten seed from the captured ledger
(`fixtures/ledger/issues.json`), so they are exercised against real gh output
rather than invented rows; the five that do not say in their own `proves` why
the capture cannot express their case. Every tick lists **every** intent that
tick emits — the comparison is exhaustive, which is what lets a scenario assert
an absence without writing `expect: []`.

`docs/m2-inputs.md` §7 named three rows of the design's failure table as covered
by no scenario at all. Two are now covered. Conflict storm: `fixtures/scenarios/rebase-storm.yaml`
proves `max_rebases` bounds it, and the row needed a rule, not the executor
signal §7 guessed at. Orphaned worktree: `fixtures/scenarios/orphaned-worktree.yaml`, seeded via
`seed.agents` — an agent `FakeExecutor` never started with a `Dispatch` or
`Continue` of its own, standing in for a worktree from outside this run's own
history (a daemon restart, a human relabelling a row while its agent keeps
running). `_reap`'s orphan pass (Task 4) already has unit coverage for the rule
itself; the scenario's marginal value is the path unit tests do not chain — a
seeded orphan retired in tick 1 through a real `expand()`/`FakeExecutor` round
trip, with tick 2 proving the freed slot is actually dispatchable again, not
merely that the right intent was returned in isolation.

**Duplicate discoveries** remains the one row genuinely out of scope for a
scenario: not an allocator rule at all — `fleet discover` searches existing
issues first, and `tests/test_discover.py` covers it. `decide()` never sees
the case, so no scenario ever will either.

## Workflows on this repository

Four workflows besides `.github/workflows/ci.yml`, all written by `agentify` (contract level 2
in `.agentify.toml`) and all unable to push to a branch:

- `.github/workflows/review.yml` and `.github/workflows/docs-review.yml`
  review every pull request with a model: one for code, one for documentation
  drift. Each job's token has `pull-requests: write` and nothing more, so a
  wrong review costs a comment and never a commit. Both read `AGENTS.md`
  first. They need one repository secret, a Claude subscription token. On
  your fork of this repository, set it with:

      gh secret set CLAUDE_CODE_AUTH_TOKEN --repo <you>/fleet

  Until it is set, both jobs fail at the model step and the PR is otherwise
  unaffected; `.github/workflows/ci.yml` is the check a merge waits on.
  Nothing is enforced server-side: no branch protection rule is
  configured.
- `.github/workflows/notify.yml` assigns the owner to any issue opened with
  the `agent-reported` or `auto-bug` label. GitHub does not notify an account
  about its own token's actions, so an issue an agent files with the owner's
  token reaches nobody until a different actor touches it.
- `.github/workflows/branch-hygiene.yml` deletes a PR's branch after merge,
  and only when the branch still points at the merged commit. A PR labelled
  `keep-branch` keeps its branch. Run it by hand with `apply` off to list the
  finished branches without deleting any.

The three labels (`agent-reported`, `auto-bug`, `keep-branch`) exist on
`FibonAdithya/fleet`; `gh issue create --label X` fails when `X` does not,
so a fresh repository needs them created first, as
`fixtures/seed/seed_issues.sh` does for the fixture repository's own
vocabulary. The labels do not carry over from
`FibonAdithya/fleet`, so a fork needs them created.

## The chain route

A target can send its `build` and `bug` classes to chain, a separate tool that
takes a task to an approved pull request on its own. fleet starts and watches
chain runs and writes the results to the ledger; it never reads chain's own
store. The route is the target's `task-classes.md` (a class whose first role has
agent kind `chain`, as in `fixtures/policy/fleet-fixture/docs/agent/task-classes.md`);
`fleet run` builds the chain executor only when some route names such a role
(`src/fleet/cli.py::build_executor`).

### fleet.toml

    [agents.chain]
    command = "chain"
    per_task_usd = 40.0

    [chain]
    default_area = "docs"
    max_age_days = 7

- `[agents.chain]`: `command` is required (the chain binary); `model_flag` is
  refused. `per_task_usd` is the cost ceiling per chain task. The shipped 40.0
  is ESTIMATE (unverified); measured chain runs cost $3.31-$18.67.
- `[chain] default_area`: the area name an adopted `bug` issue is filed under.
  Unset, nothing is adopted. `policy.load` refuses a name that is not an area
  of the ownership map.
- `[chain] max_age_days`: how old an `agent-reported` issue may be and still be
  adopted. Default 7; a whole number, at least 1.
- `[daemon] answers`: where the box writes the owner's answers. Default
  `~/.local/state/fleet/answers`. The file for a task is
  `<answers_dir>/<owner>__<name>/<number>.json`, a JSON list of
  `{"text": str, "at": float}`. The box writes it; fleet only reads it, and
  shows the answers to a native role in its prompt. The box also labels the
  issue `fleet:answered-N`, and each attempt, fix-pass and rebase cap is then
  multiplied by `1 + N`.

### How a task gets there

- A `build` issue names its spec on a line that is exactly `Spec: <path>`.
- `fleet discover --class bug|build` on a chain-routed class files the new
  issue `fleet:ready` and `fleet:auto-ok`, so it runs unattended. A `patch`, or
  a class not routed to chain, stays in `fleet:triage`. A `build` discovery
  needs `--spec`, committed on the integration branch.
- The daemon adopts an open `agent-reported` issue as a `bug` when it has no
  state label, was opened by the gh login, is no older than `max_age_days`, and
  `bug` routes to chain with `default_area` set. An issue that already carries
  another class or area label is never adopted. `src/fleet/decide.py::_adopt`
  has the full conditions.

### State file

The executor keeps a task-map file, chain-runs.json, next to the run log: which chain run is
which task, and which of a finished run's ledger writes have landed, so a
restart neither loses a live run nor writes an outcome twice. If the file
exists but cannot be read, `fleet run` stops with `ChainStateError` naming it.
Fix it by hand or remove it, but check `chain status` first: an empty map would
orphan live runs that keep spending.

### How outcomes map

| chain's outcome | fleet writes |
|---|---|
| `approved` | a handoff to `review`, then an approved verdict, so the task moves to `integrating` |
| `escalated` | an escalation to `fleet:human` carrying chain's question, with the issue left unassigned |
| `not_reproduced`, `duplicate` | a comment, `fleet:done`, and the issue closed |
| anything else (aborted, ended without an outcome, `waiting`) | the agent reads as dirty; the task is retried within its caps |
| an outcome fleet cannot record | an escalation written by fleet, naming the run |

An escalation comment starts with the exact line
`<!-- fleet:escalation reason=<reason> -->`. A run's cost counts toward the
night when its outcome is reaped, not while it runs.

### Failure lines

These appear in the daemon's failure record and `fleet status`:

- `chain run <id> is <status> but fleet has no record of starting it`: a live run
  for this repository that no task maps to, usually a start that timed out. Its
  spend is not counted. Stop it with the `chain abort <id>` command the line
  gives.
- `no route runs chain, but <path> maps runs (...)`: the route was removed while
  runs were mapped. fleet will neither stop nor report them; check
  `chain status` and abort them by hand.
- `chain run <id> is waiting on its owner`: a fleet-mode run never asks; the
  task reads dirty.
- `chain status exited ...` or `output unreadable`: the last good status is
  kept, so nothing is released on one bad read.

## Known warts

- **Rejected commands print a Python traceback** rather than a clean
  `Error: ...` line. The contract is correct — exit code 1 and nothing written —
  and the exception message is the last line, but the output is noisier than it
  should be. Converting `ValueError`/`SchemaError`/`DuplicateDiscovery` into
  `click.ClickException` would fix it.
- **SSH is unusable in this sandbox**, so `fleet-fixture`'s remote is HTTPS with
  a repo-local credential helper (`credential.https://github.com.helper =
  !gh auth git-credential`). A fresh clone needs the same treatment.
- **`search_titles` is capped at 200 issues** and filters client-side, so
  duplicate detection is best-effort, not exhaustive.

## Resetting after a graded run

Capture the ledger **before** anything below runs, or the snapshot records a
state no live run produces:

    cd ~/TIG/fleet
    gh issue list --repo "${FLEET_FIXTURE_REPO:?set it to <you>/fleet-fixture}" --state all --limit 200 \
      --json assignees,body,labels,number,state,title > fixtures/ledger/issues.json

Then reset:

    cd ~/TIG/fleet-fixture
    git checkout main && git branch -D $(git branch --list 'fleet/*') 2>/dev/null
    gh issue list --search "SMOKE" --state all --json number --jq '.[].number' \
      | xargs -r -I{} gh issue close {} --reason "not planned"
    gh label list --limit 60 --json name --jq '.[].name' \
      | grep '^discovered-by:' | xargs -r -I{} gh label delete {} --yes

Then re-seed. Do **not** bulk-close by anything looser than an exact search —
the eleven graded issues are numbers 1–11 and must survive.

**None of the above touches `.fleet/run.jsonl`.** The reset above (and
`fixtures/seed/reset.sh`) resets the issue tracker and the local git state,
not the daemon's run log — that file is not part of what a graded run is
graded on, so nothing here clears it. `fleet run` resumes by default (see
"Running the daemon" above), so starting run 2 with a bare `fleet run` after
this reset rejoins run 1's run id and inherits whatever run 1 already spent —
against a re-seeded, unspent backlog that can mean draining on the very first
tick. It is loud: the startup line names the run id as `(resumed from the run
log)` and gives the inherited `spent=$...`, and a drained run dispatches
nothing rather than silently overspending. But an operator who does not know
`--new-run` exists loses the run to it anyway. Start run 2 with:

    cd ~/TIG/fleet-fixture && fleet run --new-run

or clear `.fleet/run.jsonl` first if a fresh id is not enough on its own.

Deleting a label strips it from every issue that carried it, which is why the
capture goes first: the checked-in `fixtures/ledger/issues.json` was taken after
this step and so shows the discovered issue without its `discovered-by:` label,
contradicting the table above. See `fixtures/ledger/README.md`.
