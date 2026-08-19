"""Undo what a graded run does to `fixtures/seed/issues.yaml`'s eleven, so the
fixture can be run again.

`seed_issues.py` is idempotent BY TITLE: a second run skips all eleven issues
outright and never restores their labels, so run 1's `fleet:done` (or
`fleet:human`, or whatever attempt/fix-pass/rebase counter it stopped on)
survives into run 2, whose allocator then sees an empty backlog. `reset()`
is the fix -- diff each of the eleven against its seeded labels, not just swap
one state label for another, so it clears counter labels too without having to
know their names.

`reset()` mutates whatever `GhClient` it is given, so it is exercised in tests
against `FakeGh` only. `main()`/`reset.sh` are the only paths that ever hand it
a `RealGh`, and running either against the real fixture repo is this task's
one hard boundary -- see the module docstring in the task brief.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from fixtures.seed.seed_issues import issue_labels

from fleet.gh import GhClient, RealGh

#: The graded backlog is exactly these numbers. `fixtures/seed/issue_numbers.
#: json` records that the fixture repo has never had an issue deleted, so row
#: N of issues.yaml (1-indexed) is issue #N -- checked by seeded_from_spec.
GRADED_ISSUES = frozenset(range(1, 12))


def reset(gh: GhClient, seeded: dict[int, list[str]]) -> None:
    """Restore issues 1-11 to `seeded`'s labels, clear their comments and
    assignee, reopen any a graded run closed, and close everything else.

    Refuses a `seeded` whose keys are not exactly 1-11 before writing anything:
    a partial map would silently leave the rest of the backlog in its post-run
    state, and the next run would look reset while grading a ledger that is
    not -- the same "validate first, a rejected payload writes nothing" rule
    `commands._post` follows for every other write path in this project.
    """
    if set(seeded) != GRADED_ISSUES:
        raise ValueError(
            f"seeded must map exactly issues {sorted(GRADED_ISSUES)}, "
            f"the graded backlog; got {sorted(seeded)}"
        )

    for n in sorted(GRADED_ISSUES):
        current = gh.get_issue(n)
        if current["state"] == "CLOSED":
            # A run that reached `done` closed this issue for real --
            # expand.py's Advance-to-`done` case appends CloseIssue -- which a
            # fresh seed never does. Reopen defensively, only when needed, so
            # reset stays idempotent regardless of whether the real `gh issue
            # reopen` errors on an issue that is already open.
            gh.reopen_issue(n)
        target = seeded[n]
        extra = [label for label in current["labels"] if label not in target]
        missing = [label for label in target if label not in current["labels"]]
        # Diffed, not a hardcoded swap of one state label: this is what clears
        # fleet:attempt-N, fleet:fix-pass-N and fleet:rebase-N counters too,
        # without reset needing to know their names or how many a run spent.
        gh.remove_labels(n, extra)
        gh.add_labels(n, missing)
        # Ownership is the allocator's to write (parent §3); a stale assignee
        # from run 1 makes run 2's first tick look like a task someone already
        # holds.
        gh.assign(n, None)
        # Every comment, not just the last one: a task that cycled implementer
        # -> review -> changes_required -> review carries more than one
        # packet, and reader.read_ledger / packet.parse_latest would read a
        # leftover one as run 2's most recent.
        for c in gh.list_comments(n):
            gh.delete_comment(c["id"])

    for row in gh.list_issues("all"):
        # Discovered issues and planner children from run 1 are live triage
        # work a fresh run never produced; run 2's scout would promote another
        # night's worth of them. Guarded by current state, the same way
        # reopen above is, so a second reset does not depend on `gh issue
        # close` tolerating an issue that is already closed.
        if row["number"] not in GRADED_ISSUES and row["state"] != "CLOSED":
            gh.close_issue(row["number"])


def seeded_from_spec(spec_path: str | Path) -> dict[int, list[str]]:
    """`issues.yaml` keyed by issue number instead of applied directly, built
    through `seed_issues.issue_labels` -- the same function `seed_issues.main`
    uses -- so the seeder and the reset cannot disagree about what "seeded"
    means.
    """
    specs = yaml.safe_load(Path(spec_path).read_text())
    return {i: issue_labels(spec) for i, spec in enumerate(specs, start=1)}


def main(repo: str, spec_path: str, gh: GhClient | None = None) -> int:
    gh = gh if gh is not None else RealGh(repo)
    reset(gh, seeded_from_spec(spec_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
