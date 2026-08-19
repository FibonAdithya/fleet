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
with a fleet-dispatched agent in it.** Whether fleet's own dispatched agents
populate `cwd` or `foreground_cwd` with the fleet worktree path is
**UNVERIFIED**. This is a gate item, not a resolved question: Task 11 must
confirm it with a real `herdr agent start` inside a fresh `fleet/<n>-`
worktree (the same `herdr agent start` this capture's own Step 1 could not run
-- see above) before `RealHerdr` is trusted to read live fleet agents
correctly.
