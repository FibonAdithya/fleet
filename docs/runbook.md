# Runbook

## Seeding or resetting the fixture backlog

    cd ~/TIG/fleet && ./fixtures/seed/seed_issues.sh

Idempotent by title. Safe to re-run.

## Agent environment

Every agent process must have these set, or packets will be attributed to
`manual`:

    FLEET_ROLE=<implementer|reviewer|invariant-reviewer|planner|integrator|scout>
    FLEET_AGENT=<stable id, e.g. fleet-3>

The CLI warns on stderr when `FLEET_AGENT` is unset. `manual` is a legitimate
value for a human at a keyboard, which is exactly why an unattended run that
silently produced it would be indistinguishable from a dispatch bug.

## The four write commands

    fleet handoff  --task N --to review --did "..." --checks "..." --command "make check"
    fleet discover --from N --title "..." --area data --evidence "path:line ..."
    fleet block    --task N --on M --did "..."
    fleet escalate --task N --question "..." --considered "..." --did "..."

`block` resolves itself when task M lands. `escalate` wakes a human and is
terminal. Choosing the wrong one is the difference between a task that fixes
itself at 3am and a task that sits in the morning queue.

`handoff` cannot reach `blocked` or `human` — it refuses, naming the right
command. A handoff to those states would carry no `blocked_on` and no
`question`, leaving a task nothing could ever resolve.

## Reading a task's history

    gh issue view N --json comments --jq '.comments[].body'

## Verified live against `FibonAdithya/fleet-fixture`

The following were exercised end to end against real GitHub, then cleaned up:

| Behaviour | Result |
|---|---|
| `handoff` posts a schema-valid packet | `fleet:claimed` → `fleet:review`, `area:` preserved, real SHAs from git |
| Invalid escalation (blank question) | exit 1, **zero** writes — comment count unchanged |
| Schema-invalid handoff (empty `did`) | exit 1, **zero** writes |
| `discover` files to triage | new issue with `fleet:triage`, `area:`, `discovered-by:N`; no `fleet:ready`, no `fleet:auto-ok` |
| `discover` creates its parentage label | `discovered-by:12` did not exist and was created on demand |
| Duplicate discovery | refused; issue count unchanged |
| Parent task during discovery | labels and comments untouched |

## Known warts

- **Rejected commands print a Python traceback** rather than a clean
  `Error: ...` line. The contract is correct — exit code 1 and nothing written —
  and the exception message is the last line, but the output is noisier than it
  should be. Converting `ValueError`/`SchemaError`/`DuplicateDiscovery` into
  `click.ClickException` would fix it.
- **SSH is unusable in this sandbox**, so `fleet-fixture`'s remote is HTTPS with
  a repo-local credential helper (`credential.https://github.com.helper =
  !gh auth git-credential`). A fresh clone needs the same treatment.
- **`search_titles` is capped at 200 issues** and filters client-side, so
  duplicate detection is best-effort, not exhaustive.

## Resetting after a graded run

    cd ~/TIG/fleet-fixture
    git checkout main && git branch -D $(git branch --list 'fleet/*') 2>/dev/null
    gh issue list --search "SMOKE" --state all --json number --jq '.[].number' \
      | xargs -r -I{} gh issue close {} --reason "not planned"
    gh label list --limit 60 --json name --jq '.[].name' \
      | grep '^discovered-by:' | xargs -r -I{} gh label delete {} --yes

Then re-seed. Do **not** bulk-close by anything looser than an exact search —
the eleven graded issues are numbers 1–11 and must survive.
