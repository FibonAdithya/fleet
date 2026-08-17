# Vendored policy layer

A copy of `FibonAdithya/fleet-fixture`'s policy layer, captured the same way
`fixtures/ledger/` captures gh output and for the same reason: a fixture that
silently tracks a live working tree is not a fixture.

| Path | Captured from |
|---|---|
| `fleet-fixture/fleet.toml` | `~/TIG/fleet-fixture/fleet.toml` |
| `fleet-fixture/docs/agent/*.md` | `~/TIG/fleet-fixture/docs/agent/` |
| `fleet-fixture/.agents/schemas/*.json` | `~/TIG/fleet-fixture/.agents/schemas/` |

Captured at `fleet-fixture` commit `7377f9e`.

## Why this exists

`tests/conftest.py` used to read `~/TIG/fleet-fixture/.agents/schemas` directly.
That is an absolute path into another repo's *working tree*, so the suite
validated against whatever happened to be checked out there. It was not
hypothetical: `fleet#1` was merged only after committing a schema change that
had been sitting uncommitted in that tree, and until it landed the suite's 121
green tests were reading a file no commit contained.

## Re-capturing

A policy change in `fleet-fixture` does not reach this copy on its own. Re-run
the copy above deliberately, and say so in the commit message.
