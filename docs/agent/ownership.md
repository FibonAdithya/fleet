# Ownership map

One live task per area. Tests live in the area of the code they cover.

| Glob | Area |
|---|---|
| `src/fleet/decide.py` | allocator |
| `src/fleet/expand.py` | allocator |
| `src/fleet/simulate.py` | allocator |
| `src/fleet/escalation.py` | allocator |
| `tests/test_decide*.py` | allocator |
| `tests/test_expand.py` | allocator |
| `tests/test_escalation.py` | allocator |
| `tests/test_simulate.py` | allocator |
| `tests/test_scenarios.py` | allocator |
| `src/fleet/ledger.py` | ledger |
| `src/fleet/reader.py` | ledger |
| `src/fleet/gh.py` | ledger |
| `tests/test_ledger.py` | ledger |
| `tests/test_reader.py` | ledger |
| `tests/test_gh.py` | ledger |
| `src/fleet/cost.py` | cost |
| `tests/test_cost.py` | cost |
| `tests/test_codex_cost.py` | cost |
| `src/fleet/executor.py` | executor |
| `src/fleet/herdr.py` | executor |
| `src/fleet/herdr_executor.py` | executor |
| `src/fleet/chain_executor.py` | executor |
| `src/fleet/git.py` | executor |
| `tests/test_executor.py` | executor |
| `tests/test_herdr*.py` | executor |
| `tests/test_chain_executor.py` | executor |
| `tests/fixtures/chain/**` | executor |
| `tests/test_git.py` | executor |
| `src/fleet/daemon.py` | daemon |
| `src/fleet/runlog.py` | daemon |
| `src/fleet/statesocket.py` | daemon |
| `src/fleet/status.py` | daemon |
| `tests/test_daemon.py` | daemon |
| `tests/test_runlog.py` | daemon |
| `tests/test_statesocket.py` | daemon |
| `tests/test_status.py` | daemon |
| `src/fleet/cli.py` | cli |
| `src/fleet/config.py` | cli |
| `src/fleet/policy.py` | cli |
| `src/fleet/model.py` | cli |
| `tests/test_cli.py` | cli |
| `tests/test_config.py` | cli |
| `tests/test_policy.py` | cli |
| `tests/test_plan.py` | cli |
| `tests/test_model.py` | cli |
| `README.md` | docs |
| `docs/runbook.md` | docs |

## Non-dispatchable paths

No task touching these is ever approved with `task`. They are AGENTS.md's
"What requires a human" list as paths. fleet does not enforce this table; the
owner does, when reading the announcement.

| Path | Why |
|---|---|
| `src/fleet/labels.py` | the label vocabulary is repository state on GitHub |
| `src/fleet/packet.py` | packet schema ids; real issues carry the current shape |
| `src/fleet/commands.py` | `ROLE_MAY_HAND_TO` and `VERDICT_ROLES`; widening either is a privilege grant |
| `src/fleet/schemas.py` | packet validation |
| `Makefile` | the gate |
| `.github/**` | CI |
| `fixtures/**` | captured real output; re-capture is deliberate |
| `pyproject.toml`, `uv.lock` | the agentify pin, `requires-python`, rfc3339-validator |
| `AGENTS.md`, `docs/agent/**`, `.agents/**`, `fleet.toml` | this layer governs the fleet; editing it widens its own authority |
