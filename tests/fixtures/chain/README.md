`status-v1.json` is a byte-for-byte copy of the chain repository's
`tests/fixtures/status/v1.json` (branch `feat/fleet-mode`, commit `c2f4e51`, which added it):
the `chain status --json` output, schema `chain.status.v1`, that `fleet.chain_executor` parses.
It is a contract fixture: re-copy it from chain when the contract changes; never hand-edit it.
