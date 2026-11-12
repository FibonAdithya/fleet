# fleet

`fleet` runs several coding agents concurrently against one target repository,
unattended, and lets work move between them: an implementer hands to a
reviewer, an agent that finds an unrelated bug files it for someone else, and an
agent that hits a wall escalates rather than guessing. The target repository's
GitHub issues are the ledger, a single daemon decides who works on what, and
agents write to the ledger only through the `fleet` CLI, which validates every
packet against the target repository's schemas before anything lands. It is
used by the person running an overnight fleet, and by the agents inside it.

If you are a coding agent, read `AGENTS.md` first. It says which document is
authoritative, what must not be broken, and what needs a human.

## Quick start

    uv sync --locked --dev      # or: make setup
    make check                  # the gate: ruff, pytest, the contract self-check
    uv run fleet --help

The CLI acts on a target repository, not on this one. From a checkout that has
a `fleet.toml` in its root or a parent:

    fleet plan          # print what the allocator would do; writes nothing
    fleet run --once    # one real tick, then exit
    fleet run           # the daemon; watch it with `fleet status`

`docs/runbook.md` walks through seeding the fixture backlog, the environment
every agent needs, the five write commands, and how to reset after a run.

## Prerequisites

- `uv` and Python 3.11 or newer.
- `gh`, authenticated against the account that owns the target repository.
- herdr, which fleet uses to start and watch each agent.
- `claude`, the Claude Code CLI.
- `codex`, optional. It is needed only for a role whose agent is codex; see
  `docs/runbook.md#the-codex-gate`.

In the shared setup fleet runs on the box VM, not on a laptop. The machine
layout is in [hermes-harness](https://github.com/FibonAdithya/hermes-harness).

## Making a repository a fleet target

A target repository carries five things. `fleet` reads them from the target's
checkout (`load_config` in `src/fleet/config.py`, `load` in
`src/fleet/policy.py`); it does not read them from this repository.

| Path | What it controls |
|---|---|
| `fleet.toml` (repo root) | `[repo]` holds the GitHub `slug` and the `integration_branch` (default `integration`). `[policy]` holds the paths to the four items below. `[budget]` holds `per_task_usd` and `fleet_usd`, both of which must be greater than zero. `[fleet]` holds `slots`, `stall_minutes`, `max_attempts`, `max_fix_passes` and `max_rebases`. `[agents.*]` gives each agent CLI its `model_flag` and optional `args`. `[prices.*]` gives per-model rates, with all four of `input`, `output`, `cache_read` and `cache_write`. An optional `[daemon]` table sets the socket, run log, worktree directory and poll interval. |
| `docs/agent/ownership.md` | A `Glob` / `Area` table. The area names are the vocabulary for `area:` labels, and the allocator runs one live task per area. Parsing is strict: a malformed row or an invalid area name stops startup. See `docs/runbook.md#declaring-agents-and-models`. |
| `docs/agent/task-classes.md` | The task classes and the role each class routes to. This file also holds the `Role \| Agent \| Model` table that declares which agent CLI and model each role runs as; every declared model needs a `[prices.*]` row in `fleet.toml`, and every declared agent needs an `[agents.*]` table. |
| `.agents/roles/*.md` | One prompt file per role. The file names (without `.md`) are the role vocabulary that routes and the `Role \| Agent \| Model` table are checked against; an empty directory is an error. |
| `.agents/schemas/*.json` | The handoff and verdict schemas that every packet is validated against before it reaches the ledger. |

`fixtures/policy/fleet-fixture/` is a worked example of the `fleet.toml` and
`docs/agent/` files, and this repository's own root files are a second one.
`agentify` does not write these files; the target's owner writes them.

## Trying it on the demo target

The demo target is `FibonAdithya/fleet-fixture`. Fork it with every branch,
because fleet branches from `origin/integration` and the web UI's default fork
copies only `main`. `gh repo fork` without `--default-branch-only` copies all
branches:

    gh repo fork FibonAdithya/fleet-fixture --clone
    gh repo edit <you>/fleet-fixture --enable-issues

In the fork, set `slug = "<you>/fleet-fixture"` under `[repo]` in `fleet.toml`,
then commit and push. Fleet reads the slug from that file, not from the
environment. Still in the fork's checkout, make your fork the default for `gh`:

    gh repo set-default <you>/fleet-fixture

`gh repo fork --clone` makes the parent, `FibonAdithya/fleet-fixture`, the
default. Without this step, any `gh` command run there without `--repo`, the
runbook's reset commands included, would act on the owner's repository.

Seed the backlog from this checkout:

    FLEET_FIXTURE_REPO=<you>/fleet-fixture ./fixtures/seed/seed_issues.sh

Then, in the fork's checkout:

    fleet plan
    fleet run --once

`fixtures/seed/seed_issues.sh` is the seeding script. `docs/runbook.md` covers
the rest, including the environment every agent needs and how to reset.

## Starting a night from Hermes

On the box, Hermes' `run_fleet` verb runs `fleet run --new-run` in the
target's checkout. The spend cap is the target's `fleet.toml`, under `[budget]`
as `fleet_usd`; fleet has no budget flag and reads the file. Fork this
repository if you want to run the box setup yourself: the box's deploy step
installs fleet from the box checkout's `origin`. The verb and the deploy step
are described in the
[hermes-harness README](https://github.com/FibonAdithya/hermes-harness).

## fleet and chain

chain takes one approved spec through plan, audit, implementation, PR and
review on a laptop. fleet runs many issues in parallel against one repository,
on the box. chain is part of
[agent-habits-kit](https://github.com/FibonAdithya/agent-habits-kit).

## Checks

    make check

That is the gate CI runs (`.github/workflows/ci.yml`). It must be green before
a change is done. Dependencies come from `uv.lock`; the development group
includes `agentify`, which `tests/test_contract.py` imports to re-check this
repository's own contract. The project requires Python 3.11 or newer; `uv`
picks the interpreter in `.python-version`.

## License

MIT. See `LICENSE`.
