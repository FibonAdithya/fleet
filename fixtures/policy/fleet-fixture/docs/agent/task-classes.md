# Task classes

| Class | Route | Output | Unattended? |
|---|---|---|---|
| `patch` | implementer → reviewer → integrator | merged commit | yes |
| `spec` | planner | a spec and child issues | yes |
| `investigation` | implementer, forbidden from writing `src/` | findings comment | yes |
| `integration` | integrator | `integration` advanced | yes |

## Where work may run

This fixture is CPU-only and has no dataset. Every class above may run
unattended on `tig-server`, because `make check` is ruff plus pytest, runs in
seconds, and needs neither GPU nor data.

There is deliberately no class in this repo that requires a training run. If one
is ever added, it is not `auto-ok` here and must route elsewhere — the rule an
agent is most likely to violate cheerfully is the one that says "this box cannot
do that".

## Attempt and fix-pass limits

- A task gets **2 dispatch attempts**. A third is never made; it goes
  NEEDS_HUMAN.
- A task gets **1 review fix pass**. Fix passes do not consume dispatch
  attempts; they are counted separately.

## Which agent and model each role runs on

| Role | Agent | Model |
|---|---|---|
| `implementer` | `claude` | `claude-opus-5` |
| `reviewer` | `claude` | `claude-opus-5` |
| `integrator` | `claude` | `claude-opus-5` |
| `planner` | `claude` | `claude-opus-5` |
| `scout` | `claude` | `claude-opus-5` |
| `invariant-reviewer` | `claude` | `claude-opus-5` |
