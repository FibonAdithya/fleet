# Sharing the workflow: fleet — Design

Date: 2026-11-12

This is the fleet part of a cross-repo design whose full text lives in
`hermes-harness` (`docs/ai/specs/2026-11-12-shared-workflow-design.md`). This
document is self-contained for this run.

## Intent

The owner runs a workflow for managing many coding agents across three repos:
Hermes (`hermes-harness`) as the Telegram front end and tool broker on an
always-on VM; `chain` on the laptop to take an approved spec through plan,
audit, implementation, PR and review; and `fleet` (this repo) to run several
agents in parallel against one repository overnight on a second VM (the
"box"). Other people should be able to set up and use the same workflow from
the repositories, and this repository is to be made public.

The reader has one laptop, two small always-on VMs (one for Hermes, one for
the box), a GitHub account and a Claude subscription.

## Scope of this run

In: scrubbing machine-specific data from fixtures, README additions, runbook
adjustments, a `LICENSE`.

Out: any change to fleet's behaviour; changes to other repositories; the
fresh-VM verification run; changing repository visibility.

## Changes

### Fixtures captured from the owner's machine

These files contain the owner's real home directory, the names of the owner's
other projects, and their branch names:

- `fixtures/herdr/snapshot-fleet.json`
- `fixtures/herdr/workspace-list.json`
- `fixtures/herdr/README.md`
- `fixtures/ledger/herdr-snapshot.json`
- `fixtures/cost/codex-subagent-parent.jsonl`
- the scratchpad paths in `fixtures/herdr/agent-fleet-worktree.json`,
  `pane-list.json`, `worktree-created.json`, `worktree-list-orphan.json`

Replace home paths with `/home/u/...` (the style `tests/test_herdr.py`
already uses), and other project names, workspace labels and branch names
that are not fleet's own with neutral placeholders. Keep the structure of each
capture intact: fleet's parsers are tested against these shapes, and a scrub
that changes shape makes the tests prove less. Session UUIDs may stay.

Update tests that assert on the old values. Add a test that fails if any
tracked file under `fixtures/`, `tests/` or `docs/runbook.md` contains a
home directory other than `/home/u/`. The test must not itself list the
removed project names.

### README.md

Keep what is there. Add:

1. Prerequisites: `uv`, Python 3.11+, `gh`, herdr, `claude`; `codex`
   optional. Where fleet runs in the shared setup (the box VM), with a link to
   the hermes-harness README at `https://github.com/FibonAdithya/hermes-harness`
   for the machine layout.
2. Making a repository a fleet target: the files fleet requires
   (`fleet.toml`, `docs/agent/ownership.md`, `docs/agent/task-classes.md`,
   `.agents/roles/*.md`, `.agents/schemas/*.json`), what each controls, and
   `fixtures/policy/fleet-fixture/` as the worked example. `agentify` does not
   write these files; say so.
3. Trying it on the demo target: fork `FibonAdithya/fleet-fixture`, seed it
   with `fixtures/seed/seed_issues.sh` and `FLEET_FIXTURE_REPO`, then
   `fleet plan` and `fleet run --once`.
4. Starting a night from Hermes: the box's `run_fleet` verb runs
   `fleet run --new-run` with the target's `fleet_usd` cap. Readers fork this
   repo, because the box's deploy step installs fleet from the box checkout's
   `origin`.
5. How fleet relates to chain (one spec through PR on the laptop, versus many
   issues in parallel on the box), and a link to
   `https://github.com/FibonAdithya/agent-habits-kit`.

Any cost or resource figure is labelled `ESTIMATE (unverified)` unless it
comes from a measurement already recorded in this repository.

### docs/runbook.md

It names `FibonAdithya/fleet-fixture` throughout as the owner's instance.
Where a reader would follow a step, make it clear the slug is their own copy
(the seed scripts already read `FLEET_FIXTURE_REPO`). Leave records of past
verified runs as they are.

### LICENSE

MIT, copyright holder `FibonAdithya`.

## Constraints

- `make check` passes, including `tests/test_contract.py` and
  `tests/test_docs_references.py`.
- No secrets, tokens or account IDs anywhere.
- The sealed oracle under `fixtures/oracle/` stays in this repository.

## Anti-goals

- Changing allocator, ledger, executor or CLI behaviour.
- Weakening a test to make a scrubbed fixture pass. If a test depended on a
  real value, change the expectation to the placeholder, not the assertion's
  strength.

## Left to the builder

- Placeholder names.
- README wording and section order within the contents above.
