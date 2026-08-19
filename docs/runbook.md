# Runbook

## Seeding or resetting the fixture backlog

    cd ~/TIG/fleet && ./fixtures/seed/seed_issues.sh

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

`--no-dry-run` swaps in the same, unwrapped `RealGh` the daemon uses instead of
the read-only wrapper above — `fleet plan` itself still only reads and renders,
it never calls `apply()`, so the flag's only visible effect today is which
client type gets built. It exists so a caller can tell, from the exit code and
the client selected, that the refusal is gone rather than silently downgraded
to dry-run behaviour with no error.

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

Worktrees for live agents are created under `[daemon] worktrees` (default
`~/.fleet/worktrees`, outside the repo on purpose — a scratch worktree under
the repo root would show up as untracked in `git status`).

`fleet:rebase-N` is a third per-task counter alongside `fleet:attempt-N` and
`fleet:fix-pass-N`: an integrator's rebase attempts are capped by
`max_rebases` exactly as an implementer's attempts are capped by
`max_attempts`, and a reviewer's fix passes by `max_fix_passes`.

## Running the scenarios

    uv run pytest tests/test_scenarios.py -v

`fixtures/scenarios/` holds one file per claim, and each names its claim in its
`proves` field. Five of the eight seed from the captured ledger
(`fixtures/ledger/issues.json`), so they are exercised against real gh output
rather than invented rows; the three that do not say in their own `proves` why
the capture cannot express their case. Every tick lists **every** intent that
tick emits — the comparison is exhaustive, which is what lets a scenario assert
an absence without writing `expect: []`.

`docs/m2-inputs.md` §7 named three rows of the design's failure table as covered
by no scenario at all. Conflict storm is now covered: `rebase-storm.yaml`
proves `max_rebases` bounds it, and the row needed a rule, not the executor
signal §7 guessed at. Two remain uncovered:

- **Duplicate discoveries** is not an allocator rule at all — `fleet discover`
  searches existing issues first, and `tests/test_discover.py` covers it.
  `decide()` never sees the case, so no scenario ever will either.
- **Orphaned worktree** has a rule now (`_reap`'s orphan pass, Task 4) but no
  scenario: `FakeExecutor` only ever puts an agent in its live set via a
  `StartAgent` effect, which only `Dispatch`/`Continue` produce, and `expand()`
  pairs every other intent with a `StopAgent` for the same task. So no sequence
  of `decide()`'s own intents can leave an agent alive on a row it has moved off
  of — the only way to get one is to seed it directly, and nothing in
  `simulate.py`'s scenario schema does that. Confirmed by asking `FakeExecutor`
  directly: scripting an outcome for a task that was never dispatched leaves
  its live set empty. Covering this row needs a scenario-seeding capability
  that does not exist yet, which is a harness change and out of scope for a
  scenario file to make unilaterally.

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
    gh issue list --repo FibonAdithya/fleet-fixture --state all --limit 200 \
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

Deleting a label strips it from every issue that carried it, which is why the
capture goes first: the checked-in `fixtures/ledger/issues.json` was taken after
this step and so shows the discovered issue without its `discovered-by:` label,
contradicting the table above. See `fixtures/ledger/README.md`.
