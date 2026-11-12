# Sharing fleet: scrub, README, runbook, LICENSE — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make this repository fit to publish: no machine-specific data in fixtures, tests or the runbook, a README that lets a stranger set fleet up, and an MIT `LICENSE`.

**Architecture:** No code under `src/` changes. Captured fixtures are scrubbed by text substitution that keeps every JSON key, array length and value type. A new test scans tracked files for foreign home directories. README, runbook and LICENSE are documentation edits checked by `tests/test_docs_references.py`.

**Tech Stack:** Python 3.11+, pytest, ruff, `uv`, `agentify` docs-reference scanner (dev dependency).

**Spec:** `docs/ai/specs/2026-11-12-share-fleet-design.md`

## Global Constraints

- `make check` passes, including `tests/test_contract.py` and `tests/test_docs_references.py`.
- No secrets, tokens or account IDs anywhere.
- The sealed oracle under `fixtures/oracle/` stays in this repository (do not touch it).
- No change to allocator, ledger, executor or CLI behaviour: nothing under `src/` is edited.
- Do not weaken a test to make a scrubbed fixture pass. If a test depended on a real value, change the expected value to the placeholder, keep the assertion's strength.
- Keep the structure of each capture intact. Session UUIDs may stay.
- Home paths become `/home/u/...`.
- The new guard test must not itself list the removed project names.
- Any cost or resource figure in README is labelled `ESTIMATE (unverified)` unless it comes from a measurement already recorded in this repository. This plan's choice: README carries no such figures at all.
- LICENSE: MIT, copyright holder `FibonAdithya`.
- Runbook: records of past verified runs stay as they are. Headings are not renamed (AGENTS.md and README cite runbook anchors).
- Stage explicit paths only; never `git add -A` / `git add .` / `git commit -a`.
- Baseline (MEASURED this session, `uv run pytest --collect-only -q` summed, and `uv run pytest -q` rc=0): 805 tests, all passing.

## Authority note

AGENTS.md lists "Re-capturing any fixture under `fixtures/`" as a human decision. This run does not re-capture: it edits string values in existing captures, which the approved spec asks for. Each fixture README must say the file is scrubbed and what was replaced (AGENTS.md invariant 4: "re-capture fixtures deliberately, saying so in the commit"), and the commit message says so too.

## Out of scope (seen, deliberately left)

- Comments and docstrings in `src/fleet/*.py` and in tests that name `project-b` as a past fleet target run (`src/fleet/commands.py`, `expand.py`, `herdr_executor.py`, `ledger.py`, `tests/test_commands.py`, `tests/test_ledger.py`, `tests/test_herdr_executor.py`). Those are records of fleet runs, not fixture captures, and the spec's scope is the fixtures. The exception is `tests/test_herdr.py`, whose references describe the herdr fixture's contents and change with it (Task 1).
- `FibonAdithya` as the GitHub login in `DEFAULT_KNOWN_LOGINS`, `fleet.toml` slugs, `pyproject.toml`/`uv.lock` git URLs: public account name and behaviour, not machine data.
- AGENTS.md and runbook text saying "this private repository": repository visibility is out of scope.
- Git history. Earlier commits still contain the unscrubbed captures, and `docs/ai/plans/2026-08-18-m3-daemon.md` and `docs/ai/plans/2026-08-19-multi-provider-cost.md` contain a home path. Scrubbing the tree does not scrub history; whether to rewrite history or publish a fresh-history copy is the owner's decision when changing visibility, which the spec puts out of scope.

## Review Focus

1. **A forked `fleet-fixture` still points at the owner's repo.** Its `fleet.toml` has `slug = "FibonAdithya/fleet-fixture"`, and `fleet plan`/`fleet run` read the slug from there (`src/fleet/config.py::load_config`), not from `FLEET_FIXTURE_REPO`. A reader who only sets the env var seeds their fork and then plans against the owner's issues. README step 3 and the runbook seeding section must tell them to edit `[repo] slug` in their fork. Pinned by Task 2 Step 1 and Task 3 Step 1 (grep checks).
2. **A fork has Issues disabled and only the default branch.** GitHub forks start with Issues off and, by default, only `main`; fleet branches from `origin/integration` (`tests/test_herdr.py` documents the `couldn't find remote ref integration` failure). README step 3 must say to enable Issues (`gh repo edit <you>/fleet-fixture --enable-issues`) and to fork with all branches (`gh repo fork FibonAdithya/fleet-fixture --clone`, which copies every branch unless `--default-branch-only` is given) or push `integration`. Pinned by Task 2 Step 1.
3. **`reset.sh` also defaults the clone path to the owner's layout** (`FLEET_FIXTURE_CLONE`, default `$HOME/TIG/fleet-fixture`). A reader resetting after a run with a checkout elsewhere gets "no directory". The runbook reset section names `FLEET_FIXTURE_CLONE`. Pinned by Task 3 Step 1.
4. **Encoded home paths.** Claude's scratchpad paths encode the home directory as `-home-<name>-...`; a guard that only looks for `/home/<name>` passes over every `/tmp/claude-1000/-home-...` path the spec lists. The guard checks both forms. Pinned by Task 1 Step 1 (`test_an_encoded_foreign_home_is_found`).
5. **A guard that scans nothing passes.** If `git ls-files` returns nothing (wrong cwd, pathspec typo), a scan over an empty list is green. The repo-wide test asserts that the scanned set includes a known fixture. Pinned by Task 1 Step 1 (`test_the_scan_covers_the_fixtures`).

---

### Task 1: Scrub captured fixtures, with a guard test

**Files:**
- Create: `tests/test_no_home_paths.py`
- Modify: `fixtures/herdr/snapshot-fleet.json`, `fixtures/herdr/workspace-list.json`, `fixtures/herdr/agent-fleet-worktree.json`, `fixtures/herdr/pane-list.json`, `fixtures/herdr/worktree-created.json`, `fixtures/herdr/worktree-list-orphan.json`, `fixtures/ledger/herdr-snapshot.json`, `fixtures/cost/codex-subagent-parent.jsonl`
- Modify: `fixtures/herdr/README.md`, `fixtures/ledger/README.md`, `fixtures/cost/README.md`
- Modify: `tests/test_herdr.py:41` and the docstring at `tests/test_herdr.py:243`
- Modify: `tests/test_codex_cost.py:206,217` (`Path("/home/nobody")` → `Path("/home/u/nobody")`; `build_providers` only builds paths under it, the tests check registry keys)
- Modify: `tests/test_cost.py:104,105,197` (synthetic project dir `-home-user-proj` → `-home-u-proj`; it is only a directory name under `tmp_path`, the assertions do not change)

**Interfaces:**
- Produces: `tests/test_no_home_paths.py::foreign_homes(text: str) -> list[str]` — every home-directory reference in `text` whose user name is not exactly `u`, in either form: `/home/<name>` and Claude's encoded `-home-<name>-`. Returns the matched substrings. Later tasks rely only on the repo-wide test staying green.

- [ ] **Step 1: Write the failing tests in `tests/test_no_home_paths.py`**

The file must not contain a literal foreign home path, or it trips its own scan (it is under `tests/`). Build every positive example by concatenation, as below. It must not name any of the removed projects.

```python
FOREIGN = "/" + "home/" + "alice/proj"          # never a literal
ENCODED = "-" + "home-" + "alice-proj"

def test_a_foreign_home_is_found():
    assert foreign_homes(f"cwd: {FOREIGN}") == ["/" + "home/alice"]

def test_the_placeholder_home_is_allowed():
    assert foreign_homes("/home/u/TIG/fleet and /home/u") == []

def test_a_name_that_merely_starts_with_u_is_foreign():
    # catches `name.startswith("u")` or a regex `(?!u)` without a boundary
    assert foreign_homes("/" + "home/uma/x") == ["/" + "home/uma"]

def test_an_encoded_foreign_home_is_found():
    assert foreign_homes(f"/tmp/claude-1000/{ENCODED}/x") == ["-" + "home-alice-"]

def test_the_encoded_placeholder_is_allowed():
    assert foreign_homes("/tmp/claude-1000/-home-u-TIG-fleet/x") == []

def test_the_scan_covers_the_fixtures():
    files = tracked_files()
    assert "fixtures/herdr/snapshot-fleet.json" in files
    assert "docs/runbook.md" in files
    assert "tests/test_no_home_paths.py" in files

def test_no_tracked_file_names_a_foreign_home():
    hits = {f: foreign_homes(text_of(f)) for f in tracked_files()}
    hits = {f: h for f, h in hits.items() if h}
    assert not hits, hits
```

Helpers in the same file:
- `REPO = Path(__file__).resolve().parents[1]`
- `tracked_files() -> list[str]`: `git -C REPO ls-files -z -- fixtures tests docs/runbook.md` via `subprocess.run(..., check=True)`, split on `\0`, drop empties.
- `text_of(rel: str) -> str`: `(REPO / rel).read_bytes().decode("utf-8", errors="replace")`.
- `foreign_homes`: two regexes, `/home/([A-Za-z0-9_.-]+)` and `-home-([A-Za-z0-9_]+)-`; keep a match only when the captured name `!= "u"`; return `m.group(0)` for the slash form and `m.group(0)` for the encoded form, in order of appearance within each form (slash-form hits first, then encoded).

Mutations these catch: `test_a_name_that_merely_starts_with_u_is_foreign` catches a prefix check; `test_an_encoded_foreign_home_is_found` catches dropping the encoded regex; `test_the_placeholder_home_is_allowed` catches requiring a trailing slash after `u` (the bare `/home/u` would be flagged); `test_the_scan_covers_the_fixtures` catches an empty or wrong pathspec; `test_no_tracked_file_names_a_foreign_home` is the actual guard.

- [ ] **Step 2: Run the tests and confirm the right ones fail**

Run: `uv run pytest tests/test_no_home_paths.py -v`
Expected: the five `foreign_homes` unit tests and `test_the_scan_covers_the_fixtures` PASS; `test_no_tracked_file_names_a_foreign_home` FAILS, listing the eight fixture files, `fixtures/herdr/README.md`, `tests/test_codex_cost.py` (`/home/nobody`) and `tests/test_cost.py` (encoded `-home-user-`). A dry run of the two regexes over the 93 tracked files in scope found exactly these 11 files (MEASURED while writing this plan).

- [ ] **Step 3: Record the shape of every capture before scrubbing**

Save this as `/tmp/shape.py` (scratch, not committed). It reduces a JSON document to its structure: keys, array lengths, value types; every string becomes `""`.

```python
import json, sys
def shape(v):
    if isinstance(v, dict):  return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):  return [shape(x) for x in v]
    if isinstance(v, str):   return ""
    return v if isinstance(v, bool) or v is None else type(v).__name__
def load(path):
    text = open(path).read()
    if path.endswith(".jsonl"):
        return [json.loads(l) for l in text.splitlines() if l.strip()]
    return json.loads(text)
print(json.dumps(shape(load(sys.argv[1])), sort_keys=True))
```

Run, for each of the eight capture files `F`:
`uv run python -I /tmp/shape.py "$F" > "/tmp/shape-before-$(basename "$F").json"`

- [ ] **Step 4: Build the replacement table from the captures (scratch, not committed)**

This plan does not list the names being removed: it is committed to a repository that is about to be public. Derive them from the captures instead and keep the table in `/tmp/scrub-table.tsv` (two columns, find TAB replace), outside the repository.

Rows, in this order (longest `find` first within each group, so no row's `find` is a substring of a later row's text that has already been replaced):

1. The owner's home: the user name in `/home/<name>/` across the captures (one distinct value). Rows: `/home/<name>` → `/home/u`, and `-home-<name>-` → `-home-u-`.
2. Other projects: every distinct `repo_name`, and every path component directly under `/home/u/` or `/home/u/TIG/` (after row group 1), in the eight captures, except this keep-list: `fleet`, `fleet-fixture`, `repo`, `TIG`, `.fleet`, `.herdr`. Each becomes `project-a`, `project-b`, … in order of first appearance, reading the files in the order of this task's **Files** list. Includes the working directory in `fixtures/cost/codex-subagent-parent.jsonl`; make that row path-anchored (`/home/u/<name>` → `/home/u/project-x`) because its name is a common English word.
3. Other projects' branches and worktrees: every path component directly after `.claude/worktrees/` or `.herdr/worktrees/project-*/`, and every `branch` value, that is not a fleet branch (`fleet/<n>-<slug>`, `integration`). Each becomes `branch-1`, `branch-2`, … in first-appearance order.
4. Remaining workspace labels: after groups 1–3, every `label` value on a workspace record (not the per-tab label `"1"`) that is not one of `fleet`, `fleet-fixture`, `fleet-<n>`, `repo`, `repo (deleted)`, `project-*`, `branch-*`. Each becomes `workspace-1`, `workspace-2`, ….
5. Terminal titles: in the two snapshot files, every agent whose `cwd` (after groups 1–3) is under a `project-*` directory. One row per such agent: find = its `terminal_title_stripped` text, replace = `Other task`. Because `terminal_title` is the glyph plus that same text, the row changes both fields; a leading status glyph and space in `terminal_title` (`✳ `, `⠂ `) stays. `grep -o '"terminal_title_stripped":"[^"]*"'` lists them: 7 and 11 distinct values before the scrub, of which 6 and 10 belong to other projects. The two that stay are fleet's own (one per file, each on a pane whose `cwd` is `/home/u/TIG/fleet`).

Print the table and check it by eye: no row's `find` is a fleet name, and every row from groups 2–4 is a name that appears only in other projects' records.

- [ ] **Step 5: Apply the table to the eight captures by raw-text substitution**

Edit the raw text in table order (do not `json.load` / `json.dump`: that reformats the file — `agent-fleet-worktree.json` is pretty-printed, the others are single-line). Leave untouched: session UUIDs, workspace/pane/terminal ids, fleet's own names and branches, the `TIG` directory component (the existing tests use `/home/u/TIG/...`).

- [ ] **Step 6: Verify the shape is unchanged and nothing named survives**

Run for each capture `F`:
`uv run python -I /tmp/shape.py "$F" | diff - "/tmp/shape-before-$(basename "$F").json" && echo "same: $F"`
Expected: `same: <file>` for all eight, no diff output.

Run: `cut -f1 /tmp/scrub-table.tsv | grep -v '^$' | grep -nFf - fixtures/herdr/*.json fixtures/ledger/herdr-snapshot.json fixtures/cost/codex-subagent-parent.jsonl`
Expected: no output. (`fixtures/ledger/issue-comments.json` keeps the public GitHub login of the fixture repo's owner; it is not machine data and is not in the table.)

- [ ] **Step 7: Update the three fixture READMEs**

Apply `/tmp/scrub-table.tsv` to the prose too: `fixtures/herdr/README.md` names the other sessions' projects in "What it contains", the owner's home path in the worktree bullet, and a project, its home path and two worktree names in "Gate item for Task 11". Re-run the Step 6 `grep -nFf` with `fixtures/herdr/README.md fixtures/ledger/README.md fixtures/cost/README.md` as the files; expected no output.

Then say what was done:
- `fixtures/herdr/README.md`: change "Raw output of `herdr api snapshot`, unmodified" to say the output was scrubbed on 2026-11-12: home directory replaced with `/home/u`, other projects' names, branch names, workspace labels and terminal titles replaced with placeholders, structure unchanged. Add one sentence at the top of each of the other herdr capture sections (`agent-fleet-worktree.json`; `worktree-created.json`/`pane-list.json`/`workspace-list.json`; `worktree-list-orphan.json`) naming what that capture had replaced.
- `fixtures/ledger/README.md`: the `herdr-snapshot.json` row says "unmodified"; change to "`herdr` CLI snapshot API; scrubbed 2026-11-12 (home directory, other projects' names, branches, labels and terminal titles replaced; structure unchanged)".
- `fixtures/cost/README.md`: in the `codex-subagent-parent.jsonl` paragraph, add that the session's working directory was replaced with its `/home/u/project-*` placeholder on 2026-11-12.

- [ ] **Step 8: Update the tests that named real values**

- `tests/test_herdr.py:41`: the `/home/u/TIG/<project>` path in `test_a_path_outside_the_worktree_root_is_not_a_task` → the same path with that project's placeholder. Same assertion (`is None`).
- `tests/test_herdr.py:243` docstring: the `(<project>, <worktree>)` parenthetical → the two placeholders.
- `tests/test_codex_cost.py:206,217`: `Path("/home/nobody")` → `Path("/home/u/nobody")`.
- `tests/test_cost.py:104,105,197`: `"-home-user-proj"` → `"-home-u-proj"` (all three occurrences, so the path written and the path read still agree).

- [ ] **Step 9: Run the full gate**

Run: `make check > /tmp/share-fleet-t1.log 2>&1; echo rc=$?; tail -5 /tmp/share-fleet-t1.log`
Expected: `rc=0`; pytest reports 812 passed (805 baseline + 7 new).

- [ ] **Step 10: Mutation-check the guard**

1. In `foreign_homes`, change the name check from `!= "u"` to `not name.startswith("u")`. Run `uv run pytest tests/test_no_home_paths.py -q`; expect `test_a_name_that_merely_starts_with_u_is_foreign` to FAIL. Restore.
2. Delete the encoded-form regex. Expect `test_an_encoded_foreign_home_is_found` to FAIL. Restore.
3. Change the pathspec to `fixture` (typo). Expect `test_the_scan_covers_the_fixtures` to FAIL. Restore.
4. `cp fixtures/herdr/pane-list.json /tmp/pane-list.scrubbed.json`, then put the unscrubbed capture back: `git show HEAD:fixtures/herdr/pane-list.json > fixtures/herdr/pane-list.json`. Expect `test_no_tracked_file_names_a_foreign_home` to FAIL naming that file. Restore with `cp /tmp/pane-list.scrubbed.json fixtures/herdr/pane-list.json` and confirm `git diff --stat fixtures/herdr/pane-list.json` matches what it was before the mutation.

- [ ] **Step 11: Commit**

```bash
git status --short
git add tests/test_no_home_paths.py tests/test_herdr.py tests/test_codex_cost.py tests/test_cost.py \
  fixtures/herdr/snapshot-fleet.json fixtures/herdr/workspace-list.json \
  fixtures/herdr/agent-fleet-worktree.json fixtures/herdr/pane-list.json \
  fixtures/herdr/worktree-created.json fixtures/herdr/worktree-list-orphan.json \
  fixtures/ledger/herdr-snapshot.json fixtures/cost/codex-subagent-parent.jsonl \
  fixtures/herdr/README.md fixtures/ledger/README.md fixtures/cost/README.md
git commit -m "fixtures: scrub machine-specific paths and other projects' names

Captures are edited in place, not re-captured: home directory -> /home/u,
other projects, workspace labels, branches and terminal titles -> neutral
placeholders. JSON structure is unchanged (keys, array lengths, value types
compared before and after). Adds a test that fails on any home directory
other than /home/u/ in fixtures/, tests/ or docs/runbook.md."
```

---

### Task 2: LICENSE and README additions

**Files:**
- Create: `LICENSE`
- Modify: `README.md` (keep every existing line; add sections)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: README sections the runbook may link to by anchor. Use these exact headings so anchors are stable: `## Prerequisites`, `## Making a repository a fleet target`, `## Trying it on the demo target`, `## Starting a night from Hermes`, `## fleet and chain`, `## License`.

- [ ] **Step 1: Write the checks that define done for the README (run them first; they fail now)**

Save as `/tmp/readme-check.sh` (scratch, not committed):

```bash
set -u; f=README.md; bad=0
need() { grep -qF -- "$1" "$f" || { echo "MISSING: $1"; bad=1; }; }
for s in '## Prerequisites' '## Making a repository a fleet target' \
  '## Trying it on the demo target' '## Starting a night from Hermes' \
  '## fleet and chain' '## License' \
  'https://github.com/FibonAdithya/hermes-harness' \
  'https://github.com/FibonAdithya/agent-habits-kit' \
  '`fixtures/policy/fleet-fixture/`' '`fixtures/seed/seed_issues.sh`' \
  'FLEET_FIXTURE_REPO' 'fleet run --new-run' 'fleet_usd' 'run_fleet' \
  '--enable-issues' 'integration' '[repo]' 'slug' 'agentify' 'origin' \
  'docs/agent/ownership.md' 'docs/agent/task-classes.md' \
  '.agents/roles/' '.agents/schemas/' 'Python 3.11' '`gh`' 'herdr' '`claude`' '`codex`'; do need "$s"; done
grep -nE '\$[0-9]|[0-9]+ ?(USD|GB|MB|hours?|minutes?)\b' "$f" | grep -v 'ESTIMATE (unverified)' && { echo "UNLABELLED FIGURE"; bad=1; }
git show HEAD:README.md | while IFS= read -r l; do [ -z "$l" ] || grep -qxF -- "$l" "$f" || { echo "DROPPED: $l"; }; done | grep . && bad=1
exit $bad
```

Run: `bash /tmp/readme-check.sh`
Expected now: non-zero exit with `MISSING:` lines.

- [ ] **Step 2: Create `LICENSE`**

The standard MIT License text (as published at opensource.org/license/mit), with the copyright line exactly `Copyright (c) 2026 FibonAdithya`.

- [ ] **Step 3: Add the README sections**

Insert after the existing "Quick start" section and before "Checks", in the order of the headings in the Interfaces block (`## License` goes last, after "Checks"). Contents, from the spec:

1. `## Prerequisites` — `uv`, Python 3.11+, `gh` (authenticated), herdr, `claude`; `codex` optional (needed only for a role whose agent is codex; see `docs/runbook.md#the-codex-gate`). In the shared setup fleet runs on the box VM, not the laptop; link `https://github.com/FibonAdithya/hermes-harness` for the machine layout.
2. `## Making a repository a fleet target` — a table of the five paths and what each controls, taken from `src/fleet/config.py::load_config` and `src/fleet/policy.py::load`:
   - `fleet.toml` (repo root): `[repo]` slug and integration branch, `[policy]` paths to the four below, `[budget]` `per_task_usd`/`fleet_usd`, `[fleet]` slots and stall/attempt/fix-pass caps, `[agents.*]` launch flags, `[prices.*]` per-model rates.
   - `docs/agent/ownership.md`: glob-to-area table that `area:` labels and area exclusion come from; parsed strictly (see `docs/runbook.md#declaring-agents-and-models`).
   - `docs/agent/task-classes.md`: task classes.
   - `.agents/roles/*.md`: one prompt per role, and the `Role | Agent | Model` table that declares which agent and model each role runs as.
   - `.agents/schemas/*.json`: the handoff and verdict schemas every packet is validated against.
   Point to `fixtures/policy/fleet-fixture/` as the worked example, and this repository's own root files as a second one. State that `agentify` does not write these files; the target's owner writes them.
   Before writing each row, read the cited function and the fixture file and describe only what the code reads; if a row above does not match the code, the code wins (AGENTS.md source-of-truth order).
3. `## Trying it on the demo target` — fork with all branches: `gh repo fork FibonAdithya/fleet-fixture --clone` (without `--default-branch-only`, gh copies every branch; the web UI's default copies only `main`, and fleet branches from `origin/integration`); enable Issues on the fork: `gh repo edit <you>/fleet-fixture --enable-issues`; in the fork, set `[repo] slug = "<you>/fleet-fixture"` in `fleet.toml`, commit and push (fleet reads the slug from there, not from the environment); from this checkout, `FLEET_FIXTURE_REPO=<you>/fleet-fixture ./fixtures/seed/seed_issues.sh`; then in the fork's checkout `fleet plan` and `fleet run --once`. Link `docs/runbook.md` for the rest.
4. `## Starting a night from Hermes` — the box's `run_fleet` verb runs `fleet run --new-run` in the target's checkout; the spend cap is the target's `fleet.toml` `[budget] fleet_usd` (fleet has no budget flag; it reads the file). Readers fork this repository, because the box's deploy step installs fleet from the box checkout's `origin`. Link the hermes-harness README for the verb and the deploy step.
5. `## fleet and chain` — chain takes one approved spec through plan, audit, implementation, PR and review on the laptop; fleet runs many issues in parallel against one repository on the box. Link `https://github.com/FibonAdithya/agent-habits-kit`.
6. `## License` — one line: MIT, see `LICENSE`.

No cost, time or resource figures (Global Constraints).

- [ ] **Step 4: Run the README checks and the docs-reference test**

Run: `bash /tmp/readme-check.sh; echo rc=$?`
Expected: `rc=0`, no output lines.

Run: `uv run pytest tests/test_docs_references.py tests/test_contract.py -v`
Expected: PASS (every backticked path in README resolves; `fleet.toml`, `docs/agent/ownership.md` etc. exist at the repo root).

- [ ] **Step 5: Commit**

```bash
git status --short
git add LICENSE README.md
git commit -m "docs: README setup for other users; MIT LICENSE"
```

---

### Task 3: Runbook — the fixture slug and clone are the reader's own

**Files:**
- Modify: `docs/runbook.md`

**Interfaces:**
- Consumes: README anchors from Task 2 (`README.md#trying-it-on-the-demo-target`).
- Produces: nothing later tasks use.

- [ ] **Step 1: Write the checks (they fail now)**

Save as `/tmp/runbook-check.sh` (scratch):

```bash
set -u; f=docs/runbook.md; bad=0
# Every runnable command naming the owner's fixture repo goes through the variable.
grep -nE '^\s+.*--repo FibonAdithya/fleet-fixture' "$f" && { echo "HARDCODED SLUG IN COMMAND"; bad=1; }
for s in 'FLEET_FIXTURE_REPO' 'FLEET_FIXTURE_CLONE' '[repo]' 'README.md#trying-it-on-the-demo-target' \
  '## Verified live against `FibonAdithya/fleet-fixture`'; do
  grep -qF -- "$s" "$f" || { echo "MISSING: $s"; bad=1; }; done
# Headings unchanged (anchors are cited elsewhere).
diff <(git show HEAD:$f | grep '^#') <(grep '^#' "$f") || { echo "HEADINGS CHANGED"; bad=1; }
exit $bad
```

Run: `bash /tmp/runbook-check.sh; echo rc=$?`
Expected now: non-zero, with `HARDCODED SLUG IN COMMAND` (line 394) and `MISSING:` lines.

- [ ] **Step 2: Edit `docs/runbook.md`**

- "Seeding or resetting the fixture backlog": open with a short paragraph: `FibonAdithya/fleet-fixture` is the owner's instance; a reader works on their own fork (see `README.md#trying-it-on-the-demo-target`), sets `FLEET_FIXTURE_REPO=<you>/fleet-fixture` for the seed and reset scripts and `FLEET_FIXTURE_CLONE` for `reset.sh`'s clone path (default `$HOME/TIG/fleet-fixture`), and sets `[repo] slug` in the fork's `fleet.toml`. Paths like `~/TIG/fleet` and `~/TIG/fleet-fixture` in the commands below are the owner's checkout locations; substitute yours. Change the command to `cd ~/TIG/fleet && FLEET_FIXTURE_REPO=<you>/fleet-fixture ./fixtures/seed/seed_issues.sh`.
- "Resetting after a graded run" (line ~394): `gh issue list --repo FibonAdithya/fleet-fixture ...` → `gh issue list --repo "$FLEET_FIXTURE_REPO" ...`.
- "Workflows on this repository" (line ~354): `gh secret set CLAUDE_CODE_AUTH_TOKEN --repo FibonAdithya/fleet` → `--repo <you>/fleet`, with "your fork of this repository" in the sentence before. Line ~370: keep that the three labels exist on `FibonAdithya/fleet`, and add that a fork needs them created.
- Leave unchanged: the heading and table of "Verified live against `FibonAdithya/fleet-fixture`", and every other record of a past run.

- [ ] **Step 3: Run the runbook checks, the guard and the docs test**

Run: `bash /tmp/runbook-check.sh; echo rc=$?`
Expected: `rc=0`.

Run: `uv run pytest tests/test_docs_references.py tests/test_no_home_paths.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git status --short
git add docs/runbook.md
git commit -m "docs(runbook): the fixture repo and clone are the reader's own"
```

---

### Task 4: Final gate and leak sweep

**Files:** none modified unless a check fails (then fix in the file the check names, and commit that file).

- [ ] **Step 1: Full gate**

Run: `make check > /tmp/share-fleet-final.log 2>&1; echo rc=$?; tail -5 /tmp/share-fleet-final.log`
Expected: `rc=0`, 812 passed.

- [ ] **Step 2: Secrets and account-ID sweep over tracked files**

Run: `git grep -nIE 'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_|sk-ant-|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|xox[baprs]-|-----BEGIN [A-Z ]*PRIVATE KEY' -- . ':!uv.lock'`
Expected: no output.

- [ ] **Step 3: Oracle untouched, `src/` untouched**

Run: `git diff --stat main...HEAD -- fixtures/oracle src`
Expected: no output.

- [ ] **Step 4: Nothing unintended in the branch**

Run: `git diff --stat main...HEAD`
Expected: only the files named in Tasks 1–3 plus this plan and the spec.
