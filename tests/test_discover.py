from __future__ import annotations

import shutil

import pytest

from conftest import FIXTURE_POLICY, git
from fleet.chain_executor import spec_of
from fleet.commands import DuplicateDiscovery, do_discover
from fleet.config import load_config
from fleet.gh import FakeGh

SPEC = "docs/ai/specs/2026-11-17-lid-design.md"
BUG_ROUTE = "| `bug` | chain → integrator |"


def _target(root, task_classes=None):
    """A copy of the vendored fixture target as a git repo whose
    `origin/integration` carries one committed spec, so `do_discover` loads
    the real policy and checks `--spec` with real git."""
    shutil.copytree(FIXTURE_POLICY, root)
    classes = root / "docs/agent/task-classes.md"
    if task_classes is not None:
        classes.write_text(task_classes(classes.read_text()))
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "test")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", ".")
    git(root, "commit", "-m", "target")
    git(root, "checkout", "-b", "integration")
    (root / SPEC).parent.mkdir(parents=True, exist_ok=True)
    (root / SPEC).write_text("# lid\n")
    git(root, "add", SPEC)
    git(root, "commit", "-m", "spec")
    git(root, "update-ref", "refs/remotes/origin/integration", "integration")
    git(root, "checkout", "main")
    return load_config(root)


@pytest.fixture()
def cfg(tmp_path):
    """The fixture target, which routes `build` and `bug` to chain."""
    return _target(tmp_path / "target")


@pytest.fixture()
def herdr_cfg(tmp_path):
    """The same target with `bug` routed to herdr roles: no chain route."""
    return _target(
        tmp_path / "herdr-target",
        lambda text: text.replace(BUG_ROUTE, "| `bug` | implementer → reviewer → integrator |"),
    )


@pytest.fixture()
def gh():
    client = FakeGh()
    client.create_issue(
        "estimate_lid should accept an explicit k",
        "body",
        ["fleet:claimed", "area:eval", "fleet:auto-ok"],
    )
    return client


def test_discovery_lands_in_triage_not_ready(cfg, gh):
    n = do_discover(
        cfg,
        gh,
        parent=1,
        title="make_synthetic shifts only dim 0",
        area="data",
        klass="patch",
        evidence="src/data/synth.py:31 sets offset[0] only",
    )
    names = gh.get_issue(n)["labels"]
    assert "fleet:triage" in names
    assert "fleet:ready" not in names


def test_discovery_never_gets_auto_ok(cfg, gh):
    n = do_discover(
        cfg,
        gh,
        parent=1,
        title="make_synthetic shifts only dim 0",
        area="data",
        klass="patch",
        evidence="src/data/synth.py:31",
    )
    assert "fleet:auto-ok" not in gh.get_issue(n)["labels"]


def test_discovery_is_linked_to_its_parent(cfg, gh):
    n = do_discover(
        cfg,
        gh,
        parent=1,
        title="make_synthetic shifts only dim 0",
        area="data",
        evidence="src/data/synth.py:31",
    )
    assert "discovered-by:1" in gh.get_issue(n)["labels"]
    assert "#1" in gh.get_issue(n)["body"]


def test_evidence_is_recorded_in_the_body(cfg, gh):
    n = do_discover(
        cfg,
        gh,
        parent=1,
        title="make_synthetic ignores dims above 0",
        area="data",
        evidence="src/data/synth.py:31 sets offset[0] only",
    )
    assert "offset[0] only" in gh.get_issue(n)["body"]


def test_parent_state_is_untouched(cfg, gh):
    before = list(gh.get_issue(1)["labels"])
    do_discover(
        cfg, gh, parent=1, title="make_synthetic ignores dims above 0", area="data", evidence="e"
    )
    assert gh.get_issue(1)["labels"] == before


def test_duplicate_title_is_refused(cfg, gh):
    do_discover(
        cfg, gh, parent=1, title="make_synthetic shifts only dim 0", area="data", evidence="e"
    )
    with pytest.raises(DuplicateDiscovery):
        do_discover(
            cfg, gh, parent=1, title="MAKE_SYNTHETIC shifts only dim 0", area="data", evidence="e"
        )


def test_a_blank_title_is_refused(cfg, gh):
    """A whitespace-only title also defeats the duplicate guard: search_titles("")
    matches every issue while the exact-match test matches none, so the scan
    always passes and every blank discovery files another untitled issue."""
    before = len(gh.list_issues("all"))
    with pytest.raises(ValueError, match="title"):
        do_discover(cfg, gh, parent=1, title="   ", area="data", evidence="e")
    assert len(gh.list_issues("all")) == before


def test_unknown_area_is_rejected(cfg, gh):
    with pytest.raises(ValueError):
        do_discover(cfg, gh, parent=1, title="anything", area="nonsense", evidence="e")


def test_a_substring_collision_is_not_a_duplicate(cfg, gh):
    """The parent issue is titled "estimate_lid should accept an explicit k".
    A bare substring check refuses any title contained in it, "k" included."""
    n = do_discover(cfg, gh, parent=1, title="k", area="data", evidence="e")
    assert gh.get_issue(n)["title"] == "k"


def test_the_parentage_label_is_created_before_it_is_applied(gh, cfg):
    """`discovered-by:<n>` is generated at runtime and is not in the repo's
    label set. Real gh refuses an unknown label and creates no issue at all, and
    FakeGh now mirrors that -- so inverting the order fails this test rather
    than passing it silently."""
    do_discover(
        cfg, gh, parent=1, title="make_synthetic ignores dims above 0", area="data", evidence="e"
    )
    assert "discovered-by:1" in gh.ensured_labels


def test_a_closed_discovery_does_not_block_re_filing_it(cfg):
    """Single-use fixture, in one line. Run 1 files the discovery and the reset
    closes it; run 2's discover then refuses as a duplicate, exits 1, and rubric
    08 fails for a reason that has nothing to do with the orchestrator."""
    gh = FakeGh()
    parent = gh.create_issue("parent", "b", ["fleet:claimed", "area:eval"])
    first = do_discover(
        cfg, gh, parent=parent, title="lid rounds early", area="eval", evidence="src/eval/lid.py:44"
    )
    gh.close_issue(first)
    again = do_discover(
        cfg, gh, parent=parent, title="lid rounds early", area="eval", evidence="src/eval/lid.py:44"
    )
    assert again != first


def test_an_open_duplicate_is_still_refused(cfg):
    """The rule that matters is unchanged: two live issues for one defect is the
    duplication the search exists to prevent. Without this, 'fix the false
    refusal' becomes 'remove the check'."""
    gh = FakeGh()
    parent = gh.create_issue("parent", "b", ["fleet:claimed", "area:eval"])
    do_discover(cfg, gh, parent=parent, title="lid rounds early", area="eval", evidence="e")
    with pytest.raises(DuplicateDiscovery):
        do_discover(cfg, gh, parent=parent, title="lid rounds early", area="eval", evidence="e")


# -- the class decides the route ------------------------------------------


def _discover(cfg, gh, **kw):
    fields = dict(
        parent=1, title="lid rounds early", area="data", evidence="src/eval/lid.py:44 rounds"
    )
    fields.update(kw)
    return do_discover(cfg, gh, **fields)


def test_discover_with_chain_route_is_ready_bug_auto_ok(cfg, gh):
    """A bug an agent finds goes straight to the chain route, unattended. The
    default class is `bug`, so an agent that passes no --class gets this."""
    n = _discover(cfg, gh)
    assert gh.get_issue(n)["labels"] == [
        "fleet:ready",
        "class:bug",
        "fleet:auto-ok",
        "area:data",
        "discovered-by:1",
    ]


def test_discover_without_chain_route_is_triage(herdr_cfg, gh):
    """With `bug` routed to herdr roles nothing is auto-ok: today's triage
    labels, plus the class so a scout need not guess it."""
    n = _discover(herdr_cfg, gh)
    assert gh.get_issue(n)["labels"] == [
        "fleet:triage",
        "area:data",
        "discovered-by:1",
        "class:bug",
    ]


def test_discover_build_child_has_spec_line_and_auto_ok(cfg, gh):
    """A planner's build child names its spec on the first body line, the line
    ChainExecutor reads, and runs unattended."""
    n = _discover(cfg, gh, klass="build", spec=SPEC)
    issue = gh.get_issue(n)
    assert issue["body"].splitlines()[0] == f"Spec: {SPEC}"
    assert spec_of(issue["body"]) == SPEC
    assert issue["labels"] == [
        "fleet:ready",
        "class:build",
        "fleet:auto-ok",
        "area:data",
        "discovered-by:1",
    ]


def test_discover_build_needs_spec_on_integration(cfg, gh):
    """chain builds from the integration branch, so a spec that exists only in
    the working tree, or not at all, is refused before anything is filed. A
    build with no --spec is refused too."""
    (cfg.root / "docs/ai/specs").mkdir(parents=True, exist_ok=True)
    (cfg.root / "docs/ai/specs/local-only.md").write_text("# not pushed\n")
    before = len(gh.list_issues("all"))
    with pytest.raises(ValueError, match="origin/integration"):
        _discover(cfg, gh, klass="build", spec="docs/ai/specs/local-only.md")
    with pytest.raises(ValueError, match="spec"):
        _discover(cfg, gh, klass="build", spec=None)
    # A directory on the branch is a tree, not a spec chain can read.
    with pytest.raises(ValueError, match="origin/integration"):
        _discover(cfg, gh, klass="build", spec="docs/ai/specs")
    assert len(gh.list_issues("all")) == before
    assert "discovered-by:1" not in gh.ensured_labels


def test_discover_patch_child_stays_in_triage(cfg, gh):
    """Review focus 6: the planner files its children with the same command. A
    patch child is not auto-ok on a target that routes `bug` to chain."""
    n = _discover(cfg, gh, klass="patch")
    names = gh.get_issue(n)["labels"]
    assert names == ["fleet:triage", "area:data", "discovered-by:1", "class:patch"]
    assert "fleet:auto-ok" not in names


def test_discover_spec_refused_for_bug(cfg, gh):
    """`Spec:` means "build this spec"; on a bug or a patch it would send
    ChainExecutor to build a spec nobody asked for. Refused, nothing filed."""
    before = len(gh.list_issues("all"))
    for klass in ("bug", "patch"):
        with pytest.raises(ValueError, match="spec"):
            _discover(cfg, gh, klass=klass, spec=SPEC)
    assert len(gh.list_issues("all")) == before


def test_discover_refuses_a_class_it_cannot_file(cfg, gh):
    with pytest.raises(ValueError, match="class"):
        _discover(cfg, gh, klass="integration")


def test_a_chain_discovery_says_it_runs_unattended(cfg, gh):
    """An issue filed ready and auto-ok must not tell its reader it awaits a
    scout's triage. The evidence is kept."""
    body = gh.get_issue(_discover(cfg, gh))["body"]
    assert "chain route" in body and "unattended" in body
    assert "triage" not in body
    assert "src/eval/lid.py:44 rounds" in body
    assert "#1" in body


def test_a_triage_discovery_keeps_the_triage_body(herdr_cfg, gh):
    body = gh.get_issue(_discover(herdr_cfg, gh))["body"]
    assert "not triaged" in body
    assert "written by a scout during triage" in body
    assert "src/eval/lid.py:44 rounds" in body
