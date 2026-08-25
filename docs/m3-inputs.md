# Inputs to the M3 gate, and out of M3 into M4.

Everything here was found by *building* M3, not by reading the plan. The first
section is the part a gate operator has to act on **before** a run, because
nothing in the code can supply it. The rest carries forward.

## 1. Prerequisites a graded run needs and the fleet cannot create

### 1.1 `origin/<integration_branch>` must exist on the remote

`RealHerdr.create_worktree` fetches `origin/<base>` and passes
`--base origin/<base>` to `herdr worktree create`. That is spec §8.2's
fresh-base rule: branching off a stale local `integration` reproduces exactly
the staleness the integration branch exists to remove, and only the
remote-tracking ref carries what the fetch brought back.

**Nothing in this project pushes.** `src/fleet/git.py`'s whole seam
(`head_sha`, `merge_base_sha`, `branch_name`) is local reads;
`.agents/roles/integrator.md` has no push step; `fixtures/seed/reset.sh` resets
a **local** `integration` ref inside `$FLEET_FIXTURE_CLONE` and cannot touch the
remote. So `origin/integration` exists only because a human or an integrator put
it there.

This is therefore a **prerequisite of the fresh-base rule, not a consequence of
it**. Before a graded run:

```bash
git -C ~/TIG/fleet-fixture push origin integration
```

`create_worktree` fails with a message naming this document rather than git's
raw `couldn't find remote ref integration`, so a run that skips it says why.

Carried to M4: either `integrator.md` gains a push step, or the daemon pushes
`integration` after a merge. Both are outward-facing changes to the fixture
repo and were deliberately held back from M3.

### 1.2 The fixture's `fleet.toml` has no `[prices]` table

`fixtures/policy/fleet-fixture/fleet.toml` — and the fixture repo it is captured
from — carries `[budget]` and `[fleet]` and no `[prices]`. An absent table
parses to an empty mapping, and since the empty-table fix an empty mapping
prices **nothing**: `cost.price_transcript` raises `UnpricedModel` for the first
model it meets, `RealHerdr.unpriced()` reports it, and `Daemon.tick` drains. A
graded run against the fixture as captured today will dispatch its first tick's
agents and then drain on the second, correctly and loudly.

That is the intended failure — the alternative was a night with the ceiling
switched off — but it means the fixture repo needs a `[prices]` table added
before a graded run, with the rates for whatever model the agents actually run
on. It is not added here: `fixtures/policy/fleet-fixture/**` is a capture, and
hand-editing it makes it stop being one.

**"Whatever model the agents run on" is not the declared model alone.** Since
subagent spend is counted, the table has to cover every model an agent's
*subagents* run too, and those appear in no policy: the `Role | Agent | Model`
table names the parent, `policy._validate_agents` checks the parent's rate at
startup, and a child's rate is checked nowhere until a transcript is read
mid-run. Measured over the 67 local sessions with subagents, 43 run a child
model the parent never ran, and every child-only model on this box is
`claude-sonnet-5` or `claude-haiku-4-5-20251001`. So a table priced strictly
from the declared column — `claude-opus-5` and nothing else, which is what
`fixtures/policy/fleet-fixture/fleet.toml` carries today — drains on the first
tick after the first task that delegates. Price the whole family the agents can
reach, not the row the policy declares.

This one cannot move to a startup error the way §1.2's parent-model check did,
because nothing before dispatch knows which subagents a prompt will spawn. What
it gets instead is a message that says which half refused: a child's refusal
reaches `unpriced` as `in subagent transcript agent-<id>.jsonl: no price for
model …`, so the drain is not mistaken for a mid-session substitution in the
parent.

### 1.3 `fixtures/seed/reset.sh` can fail, and now says so

The git-side reset used to report `Reset complete` whether or not anything
worked. A reset that left run 1's `fleet/*` branches behind looked identical to
one that succeeded — and `RealHerdr._existing_branch` then finds those branches
on run 2 and skips `--base`, starting run 2's implementer on run 1's commits.
That is a false green on the gate, which is worse than a failure.

Every git-side step is now checked, failures are named on stderr, the branch
deletion is verified rather than assumed, and the script exits non-zero with an
explicit `Reset INCOMPLETE` when any of it did not hold. The issue-side reset
(`fixtures/seed/reset.py`) already failed loudly and is unchanged.

The script still resets only the **local** `integration` ref, and says so.
`origin/integration` is left where the run left it, because this project has no
push and inventing one here would push a graded run's merges to the remote from
a cleanup script. Resetting the remote is a human's step:

```bash
git -C ~/TIG/fleet-fixture push --force-with-lease origin integration:integration
```

### 1.4 `--new-run` between the two gate runs

`fleet run` resumes the run log's last `run_id` by default rather than
minting a fresh one — deliberate, so a restart mid-night recovers the
spend it already made rather than handing the fleet a second full budget.
But nothing in §1.3's reset touches `.fleet/run.jsonl`: `reset.sh` resets the
issue tracker and the local git state and says nothing about the run log,
because the run log is not part of what a graded run is graded on. So a bare
`fleet run` for the second gate run rejoins the first run's id and starts
already carrying its spend, against a freshly reset, unspent backlog — which
can drain on the very first tick. The daemon says so loudly on its startup
line (`run_id=... (resumed from the run log) spent=$...`) and a drained run
dispatches nothing rather than silently overspending, but an operator who
does not already know to reach for `--new-run` loses the run to it anyway.

Between the two graded runs:

```bash
cd ~/TIG/fleet-fixture && fleet run --new-run
```

or clear `.fleet/run.jsonl` before the second run if a fresh run id is not
enough on its own.

## 2. Carried into M4

- **Issue 6 is unprotected.** Spec §6 states it: M3 adds the merge and not the
  mechanical guard rejecting a diff that touches `Makefile`,
  `.github/workflows/`, `gates/`, `docs/agent/` or `.agents/`, or that deletes
  tests net. Issue 6 must not be in the backlog for M3's slice.
- **`Advance(to=...)` is nearly unreachable in the real shell.** Every agent
  ends a stage by running `fleet handoff`, and `commands._post` moves the label
  itself — so by the next tick the row has already moved and `_advance`, which
  is reachable only from the `claimed` branch, never runs. `Advance` fires only
  in the one-tick race where the packet is visible before the label has caught
  up. `Close` was added for the DONE case (spec §6); the same reasoning says the
  scenarios that assert `Advance(n, review)` are modelling a race rather than
  the ordinary path. M4 should decide whether `_advance` earns its place.
- **Per-agent memory is still unmeasured.** Spec §12 asks for a figure and no
  M3 task produces one, because no unattended run has been made yet.
