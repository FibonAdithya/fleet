# Task classes

| Class | Route | Output | Unattended? |
|---|---|---|---|
| `patch` | implementer → reviewer → integrator | merged commit | yes |
| `spec` | planner | a spec and child issues | yes |
| `investigation` | implementer, forbidden from writing `src/` | findings comment | yes |
| `integration` | integrator | `integration` advanced | yes |
| `build` | chain → integrator | merged commit | yes |
| `bug` | chain → integrator | merged commit | yes |

## Where work may run

fleet's gate is `make check`: ruff and pytest, CPU only, under a minute. Every
class may run unattended on `tig-server`.

## Attempt and fix-pass limits

- A task gets **2 dispatch attempts**. A third is never made; it goes
  NEEDS_HUMAN.
- A task gets **1 review fix pass**. Fix passes do not consume dispatch
  attempts; they are counted separately.

## Which agent and model each role runs on

| Role | Agent | Model |
|---|---|---|
| `implementer` | `claude` | `claude-sonnet-5` |
| `reviewer` | `claude` | `claude-sonnet-5` |
| `integrator` | `claude` | `claude-sonnet-5` |
| `planner` | `claude` | `claude-sonnet-5` |
| `scout` | `claude` | `claude-sonnet-5` |
| `invariant-reviewer` | `claude` | `claude-sonnet-5` |
| `chain` | `chain` | `chain` |
