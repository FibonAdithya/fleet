from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fixtures.seed.reset import main as reset_main
from fixtures.seed.reset import reset, seeded_from_spec
from fixtures.seed.seed_issues import main as seed_main

from fleet.gh import FakeGh

SEEDED = {n: ["fleet:ready", "area:eval", "class:patch", "fleet:auto-ok"] for n in range(1, 12)}

SPEC = Path(__file__).resolve().parents[1] / "fixtures/seed/issues.yaml"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _after_a_run() -> FakeGh:
    """Eleven graded issues in their post-run state, plus what a run leaves
    behind: a discovery, a planner child, and packets on the issues."""
    gh = FakeGh()
    for n in range(1, 12):
        gh.create_issue(f"issue {n}", "body", ["fleet:done", "area:eval", "class:patch"])
        gh.comment(n, '```json\n{"schema": "fleet.handoff.v1"}\n```')
    gh.ensure_label("discovered-by:8")
    gh.create_issue("a discovery", "body", ["fleet:triage", "area:data", "discovered-by:8"])
    gh.create_issue("a planner child", "body", ["fleet:ready", "area:models"])
    return gh


def test_reset_restores_the_seeded_labels_on_the_eleven_issues():
    """seed_issues.py is idempotent BY TITLE, so run 2 skips all eleven and
    never restores their labels: after run 1 they carry fleet:done or
    fleet:human, and run 2's allocator sees an empty backlog. Spec §1 names
    reproducibility as one of three reasons the fixture exists."""
    gh = _after_a_run()
    reset(gh, SEEDED)
    assert sorted(gh.get_issue(1)["labels"]) == sorted(SEEDED[1])
    assert "fleet:done" not in gh.get_issue(1)["labels"]


def test_reset_closes_everything_numbered_above_eleven():
    """Discovered issues and planner children from run 1 survive as live triage
    work, and run 2's scout would promote another night's worth of them."""
    gh = _after_a_run()
    reset(gh, SEEDED)
    assert gh.get_issue(12)["state"] == "CLOSED"
    assert gh.get_issue(13)["state"] == "CLOSED"


def test_reset_leaves_issues_one_to_eleven_open():
    """The eleven graded issues must survive. The runbook is explicit that
    bulk-closing by anything looser than an exact search is how they would not,
    and a reset that closes them destroys the fixture rather than resetting it."""
    gh = _after_a_run()
    reset(gh, SEEDED)
    assert [i["number"] for i in gh.list_issues("open")] == list(range(1, 12))


def test_reset_removes_run_comments_but_not_the_issue_bodies():
    """A packet from run 1 read by run 2's allocator would advance a task on
    work from a different night -- the same defect the attempt binding closes
    within a run, across runs."""
    gh = _after_a_run()
    bodies = {n: gh.get_issue(n)["body"] for n in range(1, 12)}
    reset(gh, SEEDED)
    assert gh.get_issue(1)["comments"] == []
    assert {n: gh.get_issue(n)["body"] for n in range(1, 12)} == bodies


def test_reset_clears_any_assignee():
    """Ownership is the allocator's to write, and a stale assignee from run 1
    makes run 2's first tick look like a task someone else already holds.

    "someone" from the brief's own snippet is rejected outright by FakeGh
    since Task 17 -- assign() now checks the login against known_logins, the
    same divergence-closing strictness close_issue/assign already carry (see
    docs/m2-inputs.md §6) -- so this uses a login FakeGh actually knows,
    FibonAdithya, DEFAULT_KNOWN_LOGINS' only member.
    """
    gh = _after_a_run()
    gh.assign(1, "FibonAdithya")
    reset(gh, SEEDED)
    assert gh.get_issue(1)["assignees"] == []


def test_reset_is_idempotent():
    """It runs before every graded run, including one that did not happen, and
    a reset that only works on a dirty ledger is one nobody can run twice."""
    gh = _after_a_run()
    reset(gh, SEEDED)
    first = {n: sorted(gh.get_issue(n)["labels"]) for n in range(1, 12)}
    reset(gh, SEEDED)
    assert {n: sorted(gh.get_issue(n)["labels"]) for n in range(1, 12)} == first


def test_reset_refuses_a_seed_map_that_is_not_the_eleven():
    """A caller passing a partial map would silently leave the rest of the
    backlog in its post-run state, and the next run would look reset while
    grading a ledger that is not."""
    with pytest.raises(ValueError):
        reset(_after_a_run(), {1: ["fleet:ready"]})


def test_reset_writes_nothing_when_the_seed_map_is_rejected():
    """`_post`'s comment is 'every write validates first; a rejected payload
    writes nothing' -- reset is a write path too. A caller retrying after a
    ValueError must find the ledger exactly as it was, not half-reset."""
    gh = _after_a_run()

    def snapshot() -> dict:
        return {
            n: (sorted(gh.get_issue(n)["labels"]), gh.get_issue(n)["comments"])
            for n in range(1, 14)
        }

    before = snapshot()
    with pytest.raises(ValueError):
        reset(gh, {1: ["fleet:ready"]})
    assert snapshot() == before


def test_reset_reopens_an_issue_a_graded_run_closed():
    """`expand.py` closes an issue's GitHub state (not just its label) the
    moment a task reaches `done` -- CloseIssue rides along on every Advance to
    `done`. A reset that only relabels leaves that issue CLOSED, which is not
    the state a fresh seed produces and is exactly what
    test_reset_leaves_issues_one_to_eleven_open exists to catch, made explicit
    here so the closed-issue path itself is covered rather than incidentally."""
    gh = _after_a_run()
    gh.close_issue(3)
    reset(gh, SEEDED)
    assert gh.get_issue(3)["state"] == "OPEN"


def test_reset_deletes_every_comment_not_just_the_first():
    """A task that went implementer -> review -> changes_required -> review
    carries more than one packet. A reset that deletes only one comment leaves
    a stale packet for reader.read_ledger / packet.parse_latest to pick up as
    run 2's most recent one."""
    gh = _after_a_run()
    gh.comment(1, '```json\n{"schema": "fleet.verdict.v1"}\n```')
    assert len(gh.get_issue(1)["comments"]) == 2
    reset(gh, SEEDED)
    assert gh.get_issue(1)["comments"] == []


def test_seeded_from_spec_covers_exactly_the_eleven():
    assert sorted(seeded_from_spec(SPEC)) == list(range(1, 12))


def test_seeded_from_spec_agrees_with_what_seed_issues_actually_creates():
    """reset.sh's `seeded` map comes from the same issues.yaml seed_issues.sh
    does, built through the same `issue_labels` helper -- this is the test that
    the two cannot disagree about what "seeded" means: seed a bare FakeGh for
    real, then check every one of the eleven's labels against what
    seeded_from_spec independently computed for that same row."""
    gh = FakeGh()
    seed_main("o/r", str(SPEC), gh)
    seeded = seeded_from_spec(SPEC)
    for n in range(1, 12):
        assert sorted(gh.get_issue(n)["labels"]) == sorted(seeded[n])


def test_reset_after_a_run_matches_a_fresh_seed_on_the_real_spec():
    """End to end, against the real fixture spec rather than the brief's
    hand-built SEEDED: a FakeGh seeded once, run through _after_a_run's
    post-run mutation, then reset -- must land on the exact labels a bare
    FakeGh gets from seed_issues.main. That equivalence is the whole point of
    a re-runnable fixture."""
    fresh = FakeGh()
    seed_main("o/r", str(SPEC), fresh)
    want = {n: sorted(fresh.get_issue(n)["labels"]) for n in range(1, 12)}

    dirty = FakeGh()
    seed_main("o/r", str(SPEC), dirty)
    for n in range(1, 12):
        dirty.add_labels(n, ["fleet:attempt-1"])
        dirty.remove_labels(n, ["fleet:ready"])
        dirty.add_labels(n, ["fleet:done"])
        dirty.comment(n, "packet")
    reset(dirty, seeded_from_spec(SPEC))
    got = {n: sorted(dirty.get_issue(n)["labels"]) for n in range(1, 12)}
    assert got == want


def test_reset_main_wires_seeded_from_spec_into_reset():
    """`main` is what `reset.sh` actually calls -- `seeded_from_spec` and
    `reset` each have their own coverage above, but nothing yet proves `main`
    wires them together correctly rather than, say, passing the wrong spec or
    swallowing reset's return."""
    gh = FakeGh()
    seed_main("o/r", str(SPEC), gh)
    gh.add_labels(1, ["fleet:done"])
    gh.remove_labels(1, ["fleet:ready"])
    assert reset_main("o/r", str(SPEC), gh) == 0
    assert "fleet:ready" in gh.get_issue(1)["labels"]
    assert "fleet:done" not in gh.get_issue(1)["labels"]


def test_reset_sh_can_actually_invoke_reset_py_as_a_module():
    """reset.sh runs `uv run python -m fixtures.seed.reset "$REPO" "$HERE/
    issues.yaml"` from the project root (see reset.sh's comment on this exact
    line). An earlier version ran `python "$HERE/reset.py"` instead --
    invoking a script *by path* sets sys.path[0] to the script's own
    directory (fixtures/seed/), never the CWD, so reset.py's `from
    fixtures.seed.seed_issues import issue_labels` could not resolve.
    Reproduced live: `uv run python fixtures/seed/reset.py` from the repo root
    fails with `ModuleNotFoundError: No module named 'fixtures'` every time,
    before reset.py ever gets as far as looking at its arguments.

    This drives the exact module path reset.sh invokes, but with zero
    arguments: reset.py's usage guard (`len(sys.argv) != 3`) then exits before
    `main` constructs a RealGh or makes a single network call, so this proves
    the import resolves without going anywhere near GitHub -- offline
    coverage for exactly the class of defect ("does the module even load
    when invoked the way the shell script invokes it") the rest of this file,
    running in-process against FakeGh, cannot see.
    """
    result = subprocess.run(
        ["uv", "run", "python", "-m", "fixtures.seed.reset"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "ModuleNotFoundError" not in result.stderr
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_reset_sh_does_not_claim_the_project_never_fetches():
    """A comment that is false is worse than no comment, because it is read.

    `reset.sh` stated in as many words that "nothing in this project ever runs
    `git push` or `git fetch`", and used that to justify resetting only local
    refs. `RealHerdr.create_worktree` falsifies the fetch half: it runs
    `git fetch origin <base>` and branches every new worktree off
    `origin/<base>`, which is the ref this script cannot reset and the one a
    graded run actually starts from. A human debugging run 2 starting on run 1's
    commits, reading that comment, goes looking in the wrong place.
    """
    script = (REPO_ROOT / "fixtures/seed/reset.sh").read_text()
    herdr = (REPO_ROOT / "src/fleet/herdr.py").read_text()
    # The fact the claim was about, asserted rather than assumed -- if the fetch
    # ever goes away, this test should be the thing that says the comment may
    # come back.
    assert '"fetch", "origin", base' in herdr
    assert "never\n    # runs `git push` or `git fetch`" not in script
    # And it must name the ref it cannot reset, or "local only" is a fact the
    # script knows and the operator does not.
    assert "origin/integration" in script
