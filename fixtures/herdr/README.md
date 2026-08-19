# `snapshot-fleet.json`

Raw output of `herdr api snapshot`, unmodified, captured on this box with a live
`fleet/1-capture` worktree registered in herdr at the moment of capture.

## What it contains

- 7 agent records (`result.snapshot.agents`), each a real running Claude Code
  session on this box (in `project-a`, `project-b` x2,
  `TIG/fleet` -- this very session, `TIG/project-c`,
  `TIG/project-d`, and `project-e`). Every one carries
  `state_change_seq`, `agent_status`, `cwd`, and `agent_session.value` -- the
  four fields `herdr.parse_snapshot` reads. `agent_status` values present:
  `idle`, `working`, `done`.
- 10 workspace records (`result.snapshot.workspaces`). Most have no `worktree`
  key at all (an ordinary, non-worktree workspace); three do, including
  `/home/u/.fleet/worktrees/1-capture` -- the throwaway `fleet/1-`
  worktree created for this capture (see below). This mix is exactly why
  `herdr.parse_snapshot` must never index `workspace["worktree"]`
  unconditionally: a real snapshot on this box is a mix of both, and the
  parser reads `agent.cwd` directly rather than going through workspaces at
  all, so it never hits that key.

## The positive path is covered by a derived case, not a captured one

None of this capture's seven agent records has a `cwd` under a fleet worktree
-- all seven are the real, independently-running sessions listed above, none
of them fleet's. `test_the_captured_snapshot_parses_to_no_fleet_agents`
(`tests/test_herdr.py`) asserts exactly that: `parse_snapshot` on this file
returns `[]`, and that is the *correct* result, not an unexercised one.

Task-number extraction's positive path is covered separately, by
`test_a_captured_agent_record_derived_to_a_fleet_path_parses_to_its_task`: it
deep-copies one genuine agent record out of this file and rewrites only its
`cwd` to a fleet worktree path, leaving `agent_status`, `state_change_seq`,
`agent_session`, and `foreground_cwd` exactly as captured. Labelled here as
well as in the test so nobody mistakes this for captured coverage of a live
fleet agent -- it is a derived case with real field shape, not a second real
capture, and it does not substitute for the still-open gate item below.

## What was NOT achieved, and why

The task called for a live agent running *inside* the `fleet/1-capture`
worktree, so that `result.snapshot.agents` would itself contain an entry whose
`cwd` falls under `~/.fleet/worktrees/`. That part was not achieved: this
sandbox's auto-mode permission classifier denies `herdr agent start` outright
(tried with `--kind claude` and `--kind codex`; both refused with "Blocked by
classifier" before the command ran), so no agent could be attached to the
worktree's pane. The workspace was created and was live in herdr for the
capture -- `result.snapshot.workspaces` contains it, with `worktree.checkout_path
== ".../.fleet/worktrees/1-capture"` -- but it holds no agent, so it does not
appear in `agents`.

Nothing in this file was hand-edited to compensate. It is `herdr api
snapshot`'s output exactly as returned, with the worktree workspace present and
agent-less. The `agents` array's fidelity claim rests entirely on the 7 real,
independently-running agents already on the box, which is why the field-shape
test (`test_the_captured_snapshot_has_the_fields_the_parser_reads`) is
satisfied without needing the worktree's own agent.

## Re-capturing

The commands below match this box's live `herdr` CLI (checked with `herdr
worktree create --help` / `herdr worktree remove --help`), which differs from
an earlier draft of this fixture's provenance: `herdr worktree remove` takes
`--workspace <ID>`, not a positional checkout path.

```bash
mkdir -p ~/.fleet/worktrees
herdr worktree create --cwd ~/TIG/fleet-fixture \
  --path ~/.fleet/worktrees/1-capture --branch fleet/1-capture --base main \
  --label "fleet-1" --no-focus --json
# note the returned workspace_id, e.g. "w3N"

herdr api snapshot > ~/TIG/fleet/fixtures/herdr/snapshot-fleet.json
python3 -c "
import json
s = json.load(open('$HOME/TIG/fleet/fixtures/herdr/snapshot-fleet.json'))['result']['snapshot']
print('agents', len(s['agents']), 'workspaces', len(s['workspaces']))
print([w['worktree']['checkout_path'] for w in s['workspaces'] if w.get('worktree')])
"

# Expected: the printed list contains '.../.fleet/worktrees/1-capture'.
# If it does not, the workspace did not register -- check `herdr worktree
# list --cwd ~/TIG/fleet-fixture --json` before going on.

herdr worktree remove --workspace <the workspace_id printed above> --json
cd ~/TIG/fleet-fixture && git worktree prune && git branch -D fleet/1-capture
```

If a live agent can be started in that worktree's pane in the environment
doing the re-capture (`herdr agent start <name> --kind claude --pane
<pane_id>`, then `herdr agent prompt <target> "..." --wait`), do that before
taking the snapshot -- it makes the capture strictly more faithful than this
one, which could not.

## Gate item for Task 11 -- which field a fleet worktree agent actually populates is UNVERIFIED

Two real agents in *this same capture* are a counter-example to reading `cwd`
alone. Both are herdr sessions in `project-b` working inside a nested
worktree, and in both, `cwd` stays pinned at the main checkout while
`foreground_cwd` tracks the worktree the agent is actually in:

```
cwd: /home/u/project-b
foreground_cwd: /home/u/project-b/.claude/worktrees/branch-1
```

(and the same pattern for the `branch-2` worktree agent, also
in this capture). `herdr.parse_snapshot` therefore resolves the task from
`cwd` first and falls back to `foreground_cwd` only when `cwd` yields no task
-- `cwd` is preferred because it is the pane's own directory and cannot be
moved by an agent wandering with `cd`; the fallback exists because, per the
evidence above, reading `cwd` alone would silently drop a genuinely running
agent with no exception -- worse than a crash, because it reads as an empty
`agents()` list rather than a failure, and downstream that means a stale slot
and a possible duplicate dispatch into an occupied worktree.

**This evidence is from `herdr`'s handling of a *different* tool's worktrees
(a plain `.claude/worktrees/...` checkout), not from a `fleet/<n>-` worktree
with a fleet-dispatched agent in it.** Task 10 therefore left it open whether
fleet's own dispatched agents populate `cwd` or `foreground_cwd` with the
worktree path. **Task 11 closed that gate item by measurement** -- see
`agent-fleet-worktree.json` below.

---

# `agent-fleet-worktree.json`

The gate item above, closed. One verbatim agent record out of `herdr api
snapshot`, wrapped in the same `result.snapshot.agents` envelope so
`parse_snapshot` can read it unchanged.

It was produced by doing exactly what `RealHerdr` does, live on this box:

```
git -C <repo> fetch origin integration
herdr worktree create --cwd <repo> --branch fleet/999-probe \
    --base origin/integration --path <root>/999-probe --label fleet-999 \
    --no-focus --json
herdr pane send-text  w3Q:p1 'export FLEET_ROLE=implementer FLEET_AGENT=fleet-999'
herdr pane send-keys  w3Q:p1 Enter
herdr agent start fleet-999 --kind claude --pane w3Q:p1 -- --permission-mode acceptEdits
herdr api snapshot
```

`echo $FLEET_AGENT` in that pane printed `fleet-999` before the agent started,
so the send-text/Enter pair does reach the shell.

**The answer: `cwd` carries the worktree.** Both fields do, and they are
identical:

```
cwd:            <root>/999-probe
foreground_cwd: <root>/999-probe
```

`herdr.parse_snapshot`'s `cwd`-first order is therefore correct for fleet's
own agents, and the `foreground_cwd` fallback is belt-and-braces for the
nested-worktree shape rather than the path fleet actually depends on.
`test_a_real_fleet_agent_reports_its_worktree_in_cwd` pins it.

Two further facts this record carries, both load-bearing:

- **No `agent_session` key at all.** A just-started agent has not written a
  transcript yet, so it has no session id and `parse_snapshot` must read it as
  cost `0.0` rather than raising. `test_a_freshly_started_agent_has_no_session_id_yet`
  pins that; a `KeyError` there would take down a whole daemon tick on the
  first poll after every dispatch.
- **`agent_status` is `blocked`.** That is claude's *trust-this-folder* prompt,
  which every brand-new worktree gets. `STATUS` maps `blocked` to `dirty`, so
  under `--permission-mode acceptEdits` a fresh dispatch reads as dirty
  immediately. See the concern recorded in the Task 11 report -- the fix is an
  `agent_args` that skips it, and it could not be verified here because the
  sandbox classifier blocks `--dangerously-skip-permissions`.

The probe worktree, its workspace, and the throwaway repo were all removed
afterwards (`herdr worktree remove --workspace w3Q --force`).

---

# `worktree-created.json`, `pane-list.json`, `workspace-list.json`

Verbatim output of, respectively:

```
herdr worktree create --cwd <repo> --branch fleet/12-lid-small-sample \
    --base origin/integration --path <root>/12-lid-small-sample \
    --label fleet-12 --no-focus --json
herdr pane list --workspace w3R
herdr workspace list
```

These are what `RealHerdr` parses, and they are the replies the `RecordingRun`
double in `tests/test_herdr.py` hands back, so the parsing is tested against
real herdr output rather than a hand-written dict.

`worktree-created.json` is the only place herdr offers the **workspace id** at
creation time (`result.workspace.workspace_id`) and the **pane id**
(`result.root_pane.pane_id`); `worktree remove` accepts nothing but the
workspace id, and `agent start` nothing but the pane id.

`workspace-list.json` is the restart path -- how a daemon with an empty memory
finds a worktree's workspace id again. It is deliberately a *messy* listing:
eight of its eleven workspaces have no `worktree` key at all, one is the source
repo's own workspace (opened as a side effect of `worktree create --cwd`, with
a checkout outside the worktree root), one is an unrelated repo, and exactly
one is the fleet worktree. A lookup that indexed `workspace["worktree"]`
unconditionally would raise on the first entry, and one that matched on
`label` would have two candidates.

---

# `worktree-list-orphan.json`

Verbatim `herdr worktree list --cwd <repo> --json`, captured on this box in a
state built deliberately: create a worktree, then **close its workspace** and
leave the checkout on disk. That is the leak `RealHerdr._checkout_of` exists to
detect.

The point is the disagreement between two herdr views of the same checkout:

```
$ herdr workspace list          # -> no entry for it at all
matches: []

$ herdr worktree list --cwd <repo> --json
 path=repo                          linked=False open_workspace_id='w3S'
 path=worktrees/12-lid-small-sample linked=True  open_workspace_id=(ABSENT)
```

Once the workspace is closed the checkout is not a workspace any more, so
`workspace list` -- and therefore `_workspace_of` -- cannot see it. The
git-level `worktree list` still does, with `open_workspace_id` simply absent.

That difference is the only way to tell the two silent cases apart:

- **nothing to remove** (no workspace, no checkout) -- a successful no-op;
- **a checkout with no workspace to remove it through** -- a leak, which
  `remove_worktree` raises `HerdrError` on, naming the task and the path.

Collapsed into one silent `return`, the second surfaces two dispatches later as
a `worktree create --path <existing dir>` failure, which points at the wrong
thing entirely.

Note `--cwd <repo>` is required: `worktree list` is scoped to one repo, and
without it herdr answers for whatever repo the daemon's own working directory
happens to sit in. The capture's own `result.source.source_workspace_id` (`w3S`)
is the source repo's workspace, not a fleet worktree; `task_of` rejects it,
which is why the fixture is a useful negative as well as a positive.

The probe worktree, both workspaces, and the throwaway repo were removed
afterwards.
