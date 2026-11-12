# Captured real fixtures

Raw output from real tools, checked in so M2's `decide()` can be tested against
what GitHub actually returns rather than only against `FakeGh`. Four of the
defects found in M0–M1 were fake/real divergences (see `docs/m2-inputs.md` §6);
these files are what a parity test reads.

| File | Captured with |
|---|---|
| `issues.json` | `gh issue list --repo FibonAdithya/fleet-fixture --state all --limit 200 --json assignees,body,labels,number,state,title` |
| `herdr-snapshot.json` | `herdr` CLI snapshot API; scrubbed 2026-11-12 (home directory, other projects' names, branches, labels and terminal titles replaced; structure unchanged) |
| `issue-comments.json` | `gh issue view 12 --repo FibonAdithya/fleet-fixture --json number,title,body,labels,state,assignees,comments` |

## Read `issues.json` with these caveats

**Issues 12 and 13 are the CLI smoke test, not backlog.** `#12 SMOKE: delete me`
was the `handoff`/`discover` target and `#13 SMOKE: discovered, delete me` is
the discovery it filed. Both are `CLOSED`. The eleven graded issues are 1–11.

**`#13` is missing its `discovered-by:12` label, and that is a capture artefact,
not fleet's behaviour.** `do_discover` does apply it — `docs/runbook.md`'s
verified-live table records the label being created on demand during that very
run, and `test_discovery_is_linked_to_its_parent` asserts it. The snapshot was
taken *after* the runbook's reset step, which runs

    gh label delete discovered-by:* --yes

and deleting a label strips it from every issue that carried it. A parity test
written from this file must not conclude that discoveries have no parentage
label; it would pass here and fail against a live run.

The reset in `docs/runbook.md` now says to re-capture before deleting those
labels, so the next capture will not have the gap.

**Label objects, not names.** `gh` returns
`[{"id": ..., "name": ..., "description": ..., "color": ...}]`; `RealGh` flattens
this to names, `FakeGh` stores names. Anything reading this file directly has to
flatten it too.

**States are `OPEN`/`CLOSED`.** Uppercase, which is why `FakeGh` stores that
casing — see `test_stored_state_matches_what_real_gh_returns`.

## `issue-comments.json`

A capture of `gh issue view` output for issue #12, the same CLI smoke-test
issue `issues.json` documents above -- chosen because, of the two issues in
the fixture repo that carry any comments at all (`#12` and `#1`, found via
`gh issue list --repo FibonAdithya/fleet-fixture --state all --limit 50
--json number,comments --jq '[.[] | select(.comments | length > 0)][0].number'`),
`#12` is the one the query returns first. It genuinely carries a handoff
packet -- no scratch issue or synthetic comment was needed. Re-capture with:

    gh issue view 12 --repo FibonAdithya/fleet-fixture \
      --json number,title,body,labels,state,assignees,comments \
      > fixtures/ledger/issue-comments.json

This is read-only: `gh issue view` makes no write and the fixture repo's
working tree is untouched. The file's `state` is `CLOSED` (see the caveat
above -- #12 is the smoke test's target, deliberately left closed) and its
one comment is an implementer's handoff to `review`. It carries no `attempt`
field, because it predates Task 7 adding `attempt` to the handoff schema --
consumers of this fixture that need an `attempt`-bearing packet must not
assume this comment's body has one.
