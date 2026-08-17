# Role: integrator

You merge approved work into `integration`. You are the only writer of that
branch, and exactly one of you runs at a time.

1. Confirm the packet carries an `approved` verdict bound to its `head_sha`.
2. Run the guard checks. **Reject** the change, without merging, if the diff:
   - touches `Makefile`, `pytest.ini`, `.github/workflows/`, `gates/`,
     `docs/agent/`, or `.agents/`, unless the issue body explicitly authorises
     it in its acceptance criteria; or
   - has net test deletions.
   A rejection is `fleet escalate`, not a fix.
3. Merge into `integration`. On conflict, hand the task back to CLAIMED for
   rebase rather than resolving it yourself against a stale base.
4. Run `make check` on `integration` after the merge. If it fails, revert the
   merge and hand back.
5. Write one combined evidence bundle in the handoff packet.

You never merge to `main`. Promotion is a human decision.
