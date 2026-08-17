# Captured real fixtures

Raw output from real tools, checked in so M2's `decide()` can be tested against
what GitHub actually returns rather than only against `FakeGh`. Four of the
defects found in M0–M1 were fake/real divergences (see `docs/m2-inputs.md` §6);
these files are what a parity test reads.

| File | Captured with |
|---|---|
| `issues.json` | `gh issue list --repo FibonAdithya/fleet-fixture --state all --limit 200 --json assignees,body,labels,number,state,title` |
| `herdr-snapshot.json` | `herdr` CLI snapshot API, unmodified |

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
