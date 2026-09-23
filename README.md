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

## Checks

    make check

That is the gate CI runs (`.github/workflows/ci.yml`). It must be green before
a change is done. Dependencies come from `uv.lock`; the development group
includes `agentify`, which `tests/test_contract.py` imports to re-check this
repository's own contract. The project requires Python 3.11 or newer; `uv`
picks the interpreter in `.python-version`.
