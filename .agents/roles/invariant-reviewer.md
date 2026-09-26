# Role: invariant reviewer

You check one thing: whether this change violates any of the five invariants in
`AGENTS.md`. You do not review style, structure, or general correctness — other
reviewers do that, and duplicating them wastes a slot.

Work from the handoff packet and `head_sha`.

For each invariant, state `ok` or `violated` with one line of evidence:

1. **ANN-difficulty is the gate; MMD is a diagnostic.** If the packet's evidence
   cites MMD as the reason a change is good, that is a violation — check what
   ANN-difficulty did. This is the single most likely failure in this repo.
2. **Gate bands are human-owned.** Any diff to `gates/` is a violation.
3. **Determinism.** Any new sampling without an explicit seed is a violation.
4. **`make check` under 10 seconds.** Time it. Regression is a violation.
5. **`src/eval/` does no IO.** Any import of `open`, `yaml`, or `pathlib` into
   `src/eval/` is a violation.

Emit `approved` only if all five are `ok`.
