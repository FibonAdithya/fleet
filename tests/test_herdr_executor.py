from __future__ import annotations

import logging
from typing import get_args

import pytest

from fleet.commands import do_handoff
from fleet.gh import FakeGh, GhError
from fleet.herdr import FakeHerdr, HerdrError
from fleet.herdr_executor import HerdrExecutor, build_prompt, slug_of
from fleet.model import Assign, CloseIssue, Effect, SetLabels, StartAgent, StopAgent
from fleet.packet import SCHEMA_IDS, render

#: The kwargs `do_handoff` needs, mirroring tests/test_commands.py's BASE.
HANDED_OVER = dict(
    role="implementer",
    agent="fleet-3",
    run="2026-08-10T02:14:00Z",
    base_sha="fb5ec5d",
    head_sha="a91c204",
    branch="fleet/21-lid",
    did="Clamped neighbour_count to at least 1",
    checks="make check: pass",
    commands=["make check"],
)


def _issue(gh, title="Fix LID on small samples", labels=("fleet:ready", "area:eval")):
    return gh.create_issue(title, "## Acceptance criteria\n\n- it works\n", list(labels))


def _executor(cfg, gh=None, herdr=None, owner="FibonAdithya"):
    return HerdrExecutor(herdr or FakeHerdr(), gh or FakeGh(), cfg, owner=owner)


def _role(cfg, name="implementer", text="ROLE-PROMPT-MARKER"):
    """Every StartAgent goes through `build_prompt`, which refuses to dispatch a
    role it has no prompt for -- so a test that starts an agent has to write the
    role it starts. The `cfg` fixture's policy tree carries schemas only."""
    cfg.roles_dir.mkdir(parents=True, exist_ok=True)
    (cfg.roles_dir / f"{name}.md").write_text(text)


def test_slug_is_short_lowercase_and_path_safe():
    """It becomes a branch name and a directory name. A slash would create a
    nested ref; a space would need quoting in every shell call downstream."""
    assert slug_of("Fix LID on small samples") == "fix-lid-on-small-samples"
    assert "/" not in slug_of("eval/lid: rounds before clamping")
    assert " " not in slug_of("  spaced   out  ")


def test_slug_is_bounded():
    """Branch names go into paths, and a 200-character issue title makes one
    that some filesystems refuse."""
    assert len(slug_of("x" * 300)) <= 48


def test_slug_never_ends_in_a_separator():
    """The cut lands mid-word, and `<n>-<slug>` with a trailing dash makes the
    branch `fleet/1-lid-` -- a different ref from the one a human would type
    when they go looking for the work."""
    assert not slug_of("x" * 47 + " tail").endswith("-")


def test_a_title_with_nothing_sluggable_still_yields_a_name():
    """`slug_of("###")` is the empty string, which makes the worktree directory
    `<n>-` and the branch `fleet/<n>-`. Both are legal and both read as
    truncated; worse, an empty slug is one edit away from a bare `<n>` path,
    and `herdr.task_of` is the only thing standing between that and a directory
    that no longer names its task."""
    assert slug_of("###") == "task"
    assert slug_of("") == "task"


def test_two_tasks_whose_titles_slug_identically_do_not_share_a_worktree(cfg):
    """`eval/lid: clamp` and `eval lid clamp` slug to the same string. What
    keeps them apart is the task number in front of it, so the collision is
    only ever within one task -- and a path built from the slug alone would put
    two agents in one checkout on one branch."""
    gh, herdr = FakeGh(), FakeHerdr()
    _role(cfg)
    first = _issue(gh, "eval/lid: clamp")
    second = _issue(gh, "eval lid clamp")
    assert slug_of("eval/lid: clamp") == slug_of("eval lid clamp")
    ex = _executor(cfg, gh, herdr)
    ex.apply([StartAgent(task=first, role="implementer", base="integration")])
    ex.apply([StartAgent(task=second, role="implementer", base="integration")])
    assert herdr.worktrees[first] != herdr.worktrees[second]


def test_set_labels_adds_before_removing(cfg):
    """expand._to_state excludes the added label from `remove` precisely so that
    add-then-remove is safe. Reversed, a failure between the two calls strands
    the task with no state label at all -- invisible to every label query."""
    gh = FakeGh()
    number = _issue(gh)
    calls = []
    gh.add_labels = lambda i, ls: calls.append(("add", tuple(ls)))
    gh.remove_labels = lambda i, ls: calls.append(("remove", tuple(ls)))
    _executor(cfg, gh).apply(
        [SetLabels(task=number, add=("fleet:claimed",), remove=("fleet:ready",))]
    )
    assert [c[0] for c in calls] == ["add", "remove"]


def test_start_agent_creates_a_worktree_assigns_and_prompts(cfg):
    gh, herdr = FakeGh(), FakeHerdr()
    _role(cfg)
    number = _issue(gh)
    _executor(cfg, gh, herdr).apply(
        [StartAgent(task=number, role="implementer", base="integration")]
    )
    assert number in herdr.worktrees
    assert gh.get_issue(number)["assignees"] == ["FibonAdithya"]
    assert [s[0] for s in herdr.started] == [number]


def test_ownership_is_written_by_the_executor_not_by_expand(cfg):
    """Parent §3 makes the allocator the sole writer of ownership, and M2 wrote
    none: `fleet-1` is not a GitHub login and expand has no way to learn the one
    gh is authenticated as. This layer does."""
    # `someone` has to be a login this fake resolves: FakeGh mirrors gh's
    # refusal of an assignee the repo has never heard of, and a fake that took
    # any string is the M1 divergence class docs/m2-inputs.md §6 warns about.
    gh, herdr = FakeGh(known_logins={"someone"}), FakeHerdr()
    _role(cfg)
    number = _issue(gh)
    _executor(cfg, gh, herdr, owner="someone").apply(
        [StartAgent(task=number, role="implementer", base="integration")]
    )
    assert gh.get_issue(number)["assignees"] == ["someone"]


def test_a_null_owner_clears_the_assignee(cfg):
    gh = FakeGh()
    number = _issue(gh)
    gh.assign(number, "FibonAdithya")
    _executor(cfg, gh).apply([Assign(task=number, owner=None)])
    assert gh.get_issue(number)["assignees"] == []


def test_stop_agent_also_removes_the_worktree(cfg):
    """A re-dispatch creates a worktree at the same path on the same branch. Left
    behind, the next `worktree create` fails and the task can never be retried --
    which turns one stall into a task that never runs again."""
    gh, herdr = FakeGh(), FakeHerdr()
    _role(cfg)
    number = _issue(gh)
    ex = _executor(cfg, gh, herdr)
    ex.apply([StartAgent(task=number, role="implementer", base="integration")])
    ex.apply([StopAgent(task=number)])
    assert herdr.stopped == [number]
    assert number in herdr.removed
    assert number not in herdr.worktrees


def test_a_task_can_be_started_again_after_it_has_been_stopped(cfg):
    """Every stage after the first comes back through the same task number: the
    reviewer's continuation, and a re-dispatch after a release. FakeHerdr
    refuses a create onto a live worktree exactly as herdr refuses the path, so
    an executor that stops without removing -- or does the two in the wrong
    order -- can start a task once and never again."""
    gh, herdr = FakeGh(), FakeHerdr()
    _role(cfg)
    _role(cfg, "reviewer", "REVIEWER-PROMPT")
    number = _issue(gh)
    ex = _executor(cfg, gh, herdr)
    ex.apply([StartAgent(task=number, role="implementer", base="integration")])
    ex.apply([StopAgent(task=number)])
    ex.apply([StartAgent(task=number, role="reviewer", base="integration")])
    assert [s[1] for s in herdr.started] == ["implementer", "reviewer"]
    assert "REVIEWER-PROMPT" in herdr.started[-1][2]


def test_close_issue_closes(cfg):
    gh = FakeGh()
    number = _issue(gh)
    _executor(cfg, gh).apply([CloseIssue(task=number)])
    assert gh.get_issue(number)["state"] == "CLOSED"


def test_every_effect_in_the_union_is_applied_rather_than_refused(cfg):
    """`apply` refuses what it does not recognise, and the union grows: M3 added
    `CloseIssue` after M2 shipped. A new effect this consumer does not handle is
    a silent hole in exactly one of three consumers, so the union is enumerated
    here rather than trusted."""
    gh, herdr = FakeGh(), FakeHerdr()
    _role(cfg)
    number = _issue(gh)
    samples = {
        SetLabels: SetLabels(task=number, add=("fleet:claimed",), remove=("fleet:ready",)),
        Assign: Assign(task=number, owner=None),
        StartAgent: StartAgent(task=number, role="implementer", base="integration"),
        StopAgent: StopAgent(task=number),
        CloseIssue: CloseIssue(task=number),
    }
    assert set(get_args(Effect)) == set(samples), "the Effect union grew; handle the new member"
    ex = _executor(cfg, gh, herdr)
    for effect in samples.values():
        ex.apply([effect])


def test_an_effect_outside_the_union_is_refused_rather_than_dropped(cfg):
    """Silently ignoring an unknown effect is how a whole intent becomes a
    no-op: the label moves, nothing starts, and the task looks dispatched."""
    with pytest.raises(ValueError, match="cannot apply"):
        _executor(cfg).apply(["not an effect"])


# --- one task's failure must not take the tick down ------------------------


class _RaisingHerdr(FakeHerdr):
    """`FakeHerdr` cannot produce the leak: it models a worktree as one dict
    entry with no separate workspace, so `remove_worktree` there is always a
    successful no-op. `RealHerdr.remove_worktree` raises when a checkout exists
    on disk with no herdr workspace to remove it through, and that path needs an
    injected failure to be covered at all."""

    def remove_worktree(self, task: int) -> None:
        raise HerdrError(f"task {task} has a worktree checkout but no herdr workspace")


def test_a_worktree_that_cannot_be_removed_does_not_kill_the_tick(cfg, caplog):
    """RealHerdr.remove_worktree raises on the leak case. Out of `apply` that
    exception takes the whole tick with it: every later effect in the batch is
    dropped, so one un-removable worktree stops the entire fleet -- and the
    effects lost are other tasks'."""
    gh, herdr = FakeGh(), _RaisingHerdr()
    _role(cfg)
    stuck, other = _issue(gh), _issue(gh, "Second task")
    ex = _executor(cfg, gh, herdr)
    ex.apply([StartAgent(task=stuck, role="implementer", base="integration")])
    with caplog.at_level(logging.WARNING):
        ex.apply([StopAgent(task=stuck), CloseIssue(task=other)])
    assert gh.get_issue(other)["state"] == "CLOSED"
    assert herdr.stopped == [stuck]
    assert any(str(stuck) in message for message in ex.failures)
    assert "no herdr workspace" in caplog.text


def test_stopping_an_agent_that_has_already_gone_does_not_kill_the_tick(cfg):
    """The ledger-drift scenario is exactly this: the snapshot shows no agent,
    decide emits Release(no_agent), and expand turns it into StopAgent. Real
    herdr answers `agent_not_found` and exits 1 for a target that is not there,
    so the effect that exists to clean up drift would instead crash on it --
    and the worktree it should have removed would still be there."""
    gh, herdr = FakeGh(), FakeHerdr()
    _role(cfg)
    number = _issue(gh)
    ex = _executor(cfg, gh, herdr)
    ex.apply([StartAgent(task=number, role="implementer", base="integration")])
    herdr._agents.pop(number)  # the agent vanished between ticks
    ex.apply([StopAgent(task=number)])
    assert number in herdr.removed, "the worktree must be removed even when the agent is gone"
    assert any(str(number) in message for message in ex.failures)


def test_a_defect_in_this_layer_is_not_mistaken_for_a_missing_worktree(cfg):
    """The tolerance is `except HerdrError`, not `except Exception`. A TypeError
    out of the same call is a bug here -- a renamed parameter, a task that is
    None -- and catching it too would turn a broken executor into a quiet one
    that logs `could not stop agent` every tick and never starts anything."""

    class Broken(FakeHerdr):
        def stop_agent(self, task: int) -> None:
            raise TypeError("stop_agent() got an unexpected keyword argument")

    with pytest.raises(TypeError):
        _executor(cfg, FakeGh(), Broken()).apply([StopAgent(task=1)])


def test_a_recorded_failure_does_not_grow_without_bound(cfg):
    """The daemon runs all night. A herdr that fails every removal would append
    once per task per tick, forever, into a list nothing ever reads."""
    ex = _executor(cfg, FakeGh(), _RaisingHerdr())
    for _ in range(500):
        ex.apply([StopAgent(task=1)])
    assert len(ex.failures) <= 64


def test_a_failure_that_is_not_herdrs_still_stops_the_batch(cfg):
    """The tolerance is scoped to cleanup, where the desired end state is often
    already true. Swallowing everything would turn a repo-wide gh outage into a
    fleet that logs quietly and dispatches nothing."""
    ex = _executor(cfg, FakeGh())
    with pytest.raises(GhError, match="no such issue"):
        ex.apply([CloseIssue(task=999)])


# --- the prompt ------------------------------------------------------------


def test_the_prompt_is_role_issue_packet_and_agents_md_and_nothing_else(cfg):
    """Parent §7.2 is exact about this: 'role prompt + issue body + the latest
    handoff packet + the repo's AGENTS.md. Nothing else.' The zero-history test
    of parent §6 is enforced by construction rather than by discipline, and every
    extra thing added here weakens it silently."""
    gh = FakeGh()
    number = _issue(gh)
    (cfg.roles_dir).mkdir(parents=True, exist_ok=True)
    (cfg.roles_dir / "implementer.md").write_text("ROLE-PROMPT-MARKER")
    (cfg.root / "AGENTS.md").write_text("AGENTS-MARKER")
    prompt = build_prompt(cfg, gh, number, "implementer")
    assert "ROLE-PROMPT-MARKER" in prompt
    assert "AGENTS-MARKER" in prompt
    assert "Acceptance criteria" in prompt


def test_the_prompt_carries_the_latest_handoff_packet(cfg):
    """The receiving role gets the packet and nothing else of the sender's --
    parent §6's zero-history test is the whole point. If it never reaches the
    prompt, every stage starts from the issue body alone and the packet the
    sender was made to write is decoration."""
    gh = FakeGh()
    number = _issue(gh)
    (cfg.roles_dir).mkdir(parents=True, exist_ok=True)
    (cfg.roles_dir / "reviewer.md").write_text("ROLE")
    gh.comment(number, "just a human talking")
    gh.comment(number, render({"schema": SCHEMA_IDS["handoff"], "task": number, "did": "OLD-WORK"}))
    gh.comment(number, render({"schema": SCHEMA_IDS["handoff"], "task": number, "did": "NEW-WORK"}))
    prompt = build_prompt(cfg, gh, number, "reviewer")
    assert "NEW-WORK" in prompt
    assert "OLD-WORK" not in prompt


def test_a_packet_posted_by_the_previous_stage_reaches_the_next_stages_prompt(cfg):
    """`fleet handoff` writes the comment and `build_prompt` reads it back, so
    the formatter and the parser are two halves of one contract -- and the
    daemon is the first thing to depend on both at once. A formatter change the
    parser does not follow makes every handoff invisible to the receiving agent,
    which would start the review stage with the issue body and no idea what was
    done."""
    gh = FakeGh()
    number = _issue(gh)
    _role(cfg, "reviewer", "REVIEWER")
    do_handoff(cfg, gh, task=number, to="review", **HANDED_OVER)
    prompt = build_prompt(cfg, gh, number, "reviewer")
    assert HANDED_OVER["did"] in prompt


def test_a_prompt_with_no_packet_yet_is_still_built(cfg):
    """The first stage of every task has no packet. Requiring one would make the
    implementer undispatchable -- and `parse_latest` answers None, not {}."""
    gh = FakeGh()
    number = _issue(gh)
    (cfg.roles_dir).mkdir(parents=True, exist_ok=True)
    (cfg.roles_dir / "implementer.md").write_text("ROLE")
    assert "ROLE" in build_prompt(cfg, gh, number, "implementer")


def test_a_missing_agents_md_is_not_fatal(cfg):
    """A repo without one is a repo without one. Raising here would make the
    daemon undeployable against any target repo that has not written it yet."""
    gh = FakeGh()
    number = _issue(gh)
    (cfg.roles_dir).mkdir(parents=True, exist_ok=True)
    (cfg.roles_dir / "implementer.md").write_text("ROLE")
    assert not (cfg.root / "AGENTS.md").exists()
    assert "ROLE" in build_prompt(cfg, gh, number, "implementer")


def test_a_missing_role_prompt_is_refused_rather_than_dispatched_blank(cfg):
    """An agent given an empty role prompt does something plausible and wrong.
    Failing the dispatch surfaces the policy error; a blank prompt buries it."""
    gh = FakeGh()
    number = _issue(gh)
    with pytest.raises(FileNotFoundError):
        build_prompt(cfg, gh, number, "no-such-role")


def test_the_started_agent_is_given_the_built_prompt(cfg):
    """`start_agent` taking a prompt it never sends would leave every agent
    staring at an empty pane, and no offline test would see it."""
    gh, herdr = FakeGh(), FakeHerdr()
    number = _issue(gh)
    (cfg.roles_dir).mkdir(parents=True, exist_ok=True)
    (cfg.roles_dir / "implementer.md").write_text("ROLE-PROMPT-MARKER")
    _executor(cfg, gh, herdr).apply(
        [StartAgent(task=number, role="implementer", base="integration")]
    )
    task, role, prompt = herdr.started[0]
    assert (task, role) == (number, "implementer")
    assert "ROLE-PROMPT-MARKER" in prompt


def test_agents_delegates_to_herdr(cfg):
    herdr = FakeHerdr()
    herdr.add_agent(task=3, status="working", change_seq=1)
    assert [a.task for a in _executor(cfg, herdr=herdr).agents()] == [3]
