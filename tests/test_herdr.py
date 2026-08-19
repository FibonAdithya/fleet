from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from fleet.cost import ClaudeProvider, UnpricedModel
from fleet.herdr import STATUS, FakeHerdr, HerdrError, error_code, parse_snapshot, task_of
from fleet.model import MULTIPLE_MODELS, AgentSpec, AgentState

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/herdr/snapshot-fleet.json"
ROOT = Path("/home/u/.fleet/worktrees")


def _snapshot(agents: list[dict]) -> dict:
    return {"result": {"snapshot": {"agents": agents}}}


def _agent(cwd: str, **overrides) -> dict:
    agent = {
        "agent_status": "working",
        "state_change_seq": 1,
        "cwd": cwd,
        "foreground_cwd": cwd,
        "agent_session": {"value": "session-id"},
        "agent": "claude",
    }
    agent.update(overrides)
    return agent


def test_a_fleet_worktree_path_yields_its_task_number():
    assert task_of("/home/u/.fleet/worktrees/12-lid-small-sample", ROOT) == 12


def test_a_path_outside_the_worktree_root_is_not_a_task():
    """A human's own herdr workspace on the same box must not be read as a fleet
    agent. It would be counted against `slots` and REAP would try to stop it."""
    assert task_of("/home/u/TIG/project-d", ROOT) is None


def test_a_worktree_with_no_leading_number_is_not_a_task():
    assert task_of("/home/u/.fleet/worktrees/scratch", ROOT) is None


def test_every_herdr_status_maps_to_a_fleet_status():
    """herdr has five statuses and AgentState has three. An unmapped one would
    KeyError out of the tick -- taking down every task's intents, not one."""
    assert set(STATUS) == {"idle", "working", "blocked", "done", "unknown"}
    assert set(STATUS.values()) <= {"working", "done", "dirty"}


def test_a_blocked_agent_is_dirty():
    """herdr `blocked` means the agent is waiting for input it will never get --
    a permission prompt, which parent §8 names as the most common stall. Waiting
    stall_minutes to notice something already known is a wasted ten minutes."""
    assert STATUS["blocked"] == "dirty"


def test_an_idle_agent_has_finished_its_turn():
    """The dispatcher submits one prompt. Back at idle, that turn is over and
    the packet -- if there is one -- has been written."""
    assert STATUS["idle"] == "done"


def test_an_unknown_status_is_treated_as_working():
    """herdr saying it cannot tell is not evidence of failure. Acting on it
    would release healthy agents; the stall timer covers a real hang."""
    assert STATUS["unknown"] == "working"


def test_a_working_agent_stays_working():
    """Pins the one mapping the aggregate key-set/value-membership test cannot:
    swapping `working` and `done` in STATUS would still pass
    `test_every_herdr_status_maps_to_a_fleet_status` (same key set, same value
    set), report a mid-turn agent as finished, and release its slot early."""
    assert STATUS["working"] == "working"


def test_a_done_agent_stays_done():
    """Same gap, the other direction: swapped, a finished agent would read as
    still working and hold a slot the fleet needs for the next dispatch."""
    assert STATUS["done"] == "done"


def test_the_fake_yields_agents_for_fleet_worktrees_only(tmp_path):
    fake = FakeHerdr()
    fake.add_agent(task=3, status="working", change_seq=7, cost_usd=0.25)
    fake.add_agent(task=9, status="done", change_seq=8)
    assert [a.task for a in fake.agents()] == [3, 9]
    assert fake.agents()[0].status == "working"
    assert fake.agents()[0].cost_usd == 0.25


# The three tests below exist because this is the first task with a real client
# to compare the fake against. docs/m2-inputs.md §6 records four M1 cases where
# FakeGh was subtly more permissive than RealGh, and every one of them passed
# the entire offline suite and failed against GitHub. These are the same shape:
# in each, RealHerdr raises and FakeHerdr used to succeed, so a caller written
# against the fake would carry a bug the offline suite could never see.


def test_the_fake_refuses_to_stop_an_agent_that_is_not_running():
    """`RealHerdr.stop_agent` shells `herdr agent send-keys fleet-<task>`, which
    answers `{"code":"agent_not_found"}` and rc=1 for a target that is not
    there, so it raises. A fake that popped a missing key and returned would
    let a double-stop -- or a stop of a task that never started -- look fine
    offline and fail on the box."""
    fake = FakeHerdr()
    with pytest.raises(HerdrError):
        fake.stop_agent(task=1)


def test_the_fake_refuses_to_start_an_agent_with_no_worktree():
    """`RealHerdr.start_agent` resolves a pane through the task's workspace and
    raises `task <n> has no herdr workspace` when there is none. The fake used
    to register a working agent regardless, so a dispatch ordering bug --
    start_agent before create_worktree -- would pass every offline test."""
    fake = FakeHerdr()
    with pytest.raises(HerdrError):
        fake.start_agent(task=1, role="implementer", prompt="x")
    assert fake.started == []
    assert fake.agents() == []


def test_the_fake_refuses_to_create_a_worktree_that_already_exists():
    """Real herdr fails a `worktree create` onto an existing path or branch.
    The fake used to overwrite its map entry and hand back the path, so a
    duplicate dispatch into an already-occupied worktree -- the precise failure
    the `foreground_cwd` fallback exists to prevent -- would look like a clean
    create offline."""
    fake = FakeHerdr()
    fake.create_worktree(task=1, slug="lid", base="integration")
    with pytest.raises(HerdrError):
        fake.create_worktree(task=1, slug="lid", base="integration")


def test_the_fake_allows_a_recreate_after_a_removal():
    """The guard above is about a *live* worktree, not a permanent ban. A task
    whose worktree was removed can be dispatched again, and real herdr allows
    it -- the path is free. Without this, the strictness added for the fake's
    sake would break the retry path instead."""
    fake = FakeHerdr()
    fake.create_worktree(task=1, slug="lid", base="integration")
    fake.remove_worktree(task=1)
    assert fake.create_worktree(task=1, slug="lid", base="integration") == fake.root / "1-lid"


def test_the_fake_stops_an_agent_it_started():
    """The positive path, so the strictness above cannot be satisfied by a fake
    that simply raises on every stop."""
    fake = FakeHerdr()
    fake.create_worktree(task=1, slug="lid", base="integration")
    fake.start_agent(task=1, role="implementer", prompt="x")
    assert [a.task for a in fake.agents()] == [1]
    fake.stop_agent(task=1)
    assert fake.stopped == [1]
    assert fake.agents() == []


def test_the_captured_snapshot_parses_to_no_fleet_agents():
    """The real assertion, not just `isinstance(parsed, list)`: an empty result
    here is the *correct* parse, not an unexercised one. None of the capture's
    seven agents runs under a fleet worktree -- they are real, independently
    running Claude Code sessions elsewhere on the box (see
    fixtures/herdr/README.md). The positive path is exercised separately, by
    a case derived from this same capture below."""
    raw = json.loads(FIXTURE.read_text())
    parsed = parse_snapshot(raw, ROOT)
    assert parsed == []


def test_a_captured_agent_record_derived_to_a_fleet_path_parses_to_its_task():
    """Real field shape, not a hand-built dict: a genuine agent record copied
    out of the committed capture, with only `cwd` rewritten to a fleet
    worktree path. Every other field -- `agent_status`, `state_change_seq`,
    `agent_session`, `foreground_cwd` -- is exactly as herdr returned it.
    Before this test, the hand-built string in
    `test_a_fleet_worktree_path_yields_its_task_number` was the only place
    `task_of` ever returned a number; this is what backs `parse_snapshot`
    itself against something herdr actually produced."""
    raw = json.loads(FIXTURE.read_text())
    agent = copy.deepcopy(raw["result"]["snapshot"]["agents"][0])
    agent["cwd"] = str(ROOT / "12-lid-small-sample")
    parsed = parse_snapshot(_snapshot([agent]), ROOT)
    assert [a.task for a in parsed] == [12]


def test_a_priced_agent_carries_its_transcript_cost(tmp_path):
    """Cost-join coverage, positive path: no existing test passed
    `projects_dir`/`prices`, so the join branch never executed."""
    session = "11111111-1111-1111-1111-111111111111"
    project = tmp_path / "projects" / "slug"
    project.mkdir(parents=True)
    (project / f"{session}.jsonl").write_text(
        json.dumps({"message": {"model": "test-model", "usage": {"input_tokens": 1_000_000}}})
        + "\n"
    )
    prices = {"test-model": {"input": 10.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}}
    agent = _agent(str(ROOT / "5-slug"), agent_session={"value": session})
    providers = {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}
    parsed = parse_snapshot(_snapshot([agent]), ROOT, providers=providers, prices=prices)
    assert parsed[0].cost_usd == pytest.approx(10.0)


def test_an_unpriced_model_propagates_out_of_parse_snapshot(tmp_path):
    """Cost-join coverage, the path that matters most: an agent whose model
    the price table does not know must not be read as free. Defaulting to
    zero here would defeat the budget ceiling silently; Task 15 is specified
    to catch `UnpricedModel` at the daemon tick and drain, which only works if
    it actually escapes `parse_snapshot` rather than being swallowed."""
    session = "22222222-2222-2222-2222-222222222222"
    project = tmp_path / "projects" / "slug"
    project.mkdir(parents=True)
    (project / f"{session}.jsonl").write_text(
        json.dumps({"message": {"model": "unpriced-model", "usage": {"input_tokens": 1}}}) + "\n"
    )
    # A non-empty table that simply lacks "unpriced-model" -- an empty dict
    # would be falsy and skip the join branch entirely, proving nothing.
    prices = {"other-model": {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0}}
    agent = _agent(str(ROOT / "5-slug"), agent_session={"value": session})
    providers = {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}
    with pytest.raises(UnpricedModel):
        parse_snapshot(_snapshot([agent]), ROOT, providers=providers, prices=prices)


def test_cwd_resolves_the_task_when_it_disagrees_with_foreground_cwd():
    """cwd wins when the two disagree: it is the pane's own directory and
    cannot be moved by an agent wandering with `cd`, unlike foreground_cwd.
    Both fields point at *different, valid* fleet worktrees on purpose --
    with foreground_cwd merely non-fleet (task_of returns None either way),
    a wrong resolution order still landed on cwd's answer by fallback, which
    would not have caught foreground_cwd being tried first."""
    agent = _agent(cwd=str(ROOT / "7-slug"), foreground_cwd=str(ROOT / "99-elsewhere"))
    parsed = parse_snapshot(_snapshot([agent]), ROOT)
    assert [a.task for a in parsed] == [7]


def test_foreground_cwd_resolves_the_task_when_cwd_does_not():
    """The fallback this task's review demanded: this box's own capture shows
    a real agent (project-b, branch-1) with `cwd` pinned at the
    main checkout and only `foreground_cwd` tracking the worktree it was
    actually in. Without this fallback, a fleet agent behaving the same way
    would vanish from `agents()` with no exception -- a stale slot and a
    possible duplicate dispatch into an occupied worktree."""
    agent = _agent(cwd="/home/u/somewhere/else", foreground_cwd=str(ROOT / "7-slug"))
    parsed = parse_snapshot(_snapshot([agent]), ROOT)
    assert [a.task for a in parsed] == [7]


def test_the_captured_snapshot_has_the_fields_the_parser_reads():
    """Pins the shape rather than the values, so a herdr upgrade that renames a
    field fails here instead of silently reporting an empty fleet."""
    raw = json.loads(FIXTURE.read_text())
    agents = raw["result"]["snapshot"]["agents"]
    assert agents, "the capture has no agents; re-capture it"
    for agent in agents:
        assert "state_change_seq" in agent
        assert "agent_status" in agent
        assert "cwd" in agent
        assert "value" in (agent.get("agent_session") or {})


# --- RealHerdr -------------------------------------------------------------
#
# RealHerdr is never executed by the offline suite, so these tests can only pin
# the argv it BUILDS. That is a weak instrument on its own -- a wrong flag
# passes every one of them and fails first against the live CLI. So the replies
# below are not hand-written: they are verbatim captures of what `herdr` on this
# box actually printed (fixtures/herdr/README.md records the session), and every
# flag asserted here was executed live at least once. Where a flag was rejected
# live, the test names the error code herdr returned.

WORKTREE_CREATED = Path(__file__).resolve().parents[1] / "fixtures/herdr/worktree-created.json"
PANE_LIST = Path(__file__).resolve().parents[1] / "fixtures/herdr/pane-list.json"
WORKSPACE_LIST = Path(__file__).resolve().parents[1] / "fixtures/herdr/workspace-list.json"
AGENT_IN_WORKTREE = Path(__file__).resolve().parents[1] / "fixtures/herdr/agent-fleet-worktree.json"
WORKTREE_LIST_ORPHAN = (
    Path(__file__).resolve().parents[1] / "fixtures/herdr/worktree-list-orphan.json"
)

# The worktree root the captures were taken under, read out of the capture
# rather than retyped: a copy-paste drift between the two would make every
# workspace-lookup test resolve to None and pass for the wrong reason.
CAPTURE_ROOT = Path(json.loads(WORKTREE_CREATED.read_text())["result"]["worktree"]["path"]).parent
CAPTURE_TASK = 12
CAPTURE_WORKSPACE = "w3R"
CAPTURE_PANE = "w3R:p1"

# The orphan capture is a *different* repo and a different worktree root: a
# checkout that exists at the git level with no herdr workspace open on it.
# Derived from the capture for the same reason as CAPTURE_ROOT.
ORPHAN_ROOT = Path(
    [
        w["path"]
        for w in json.loads(WORKTREE_LIST_ORPHAN.read_text())["result"]["worktrees"]
        if w["is_linked_worktree"]
    ][0]
).parent


class RecordingRun:
    """Captures argv instead of running it.

    Replies are keyed by argv *prefix*, not by `args[0]`: every herdr call
    starts with the literal "herdr", so a first-element key cannot tell
    `worktree create` from `pane list` and would hand the same reply to both.
    """

    def __init__(self, replies=None):
        self.calls: list[tuple[str, ...]] = []
        self.replies = dict(replies or {})

    def __call__(self, *args):
        self.calls.append(args)
        for prefix, reply in self.replies.items():
            if args[: len(prefix)] == prefix:
                return reply
        return ""

    def herdr_calls(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == prefix]

    @property
    def flat(self):
        return " ".join(" ".join(c) for c in self.calls)


def _captured_replies():
    return {
        ("herdr", "worktree", "create"): WORKTREE_CREATED.read_text(),
        ("herdr", "pane", "list"): PANE_LIST.read_text(),
        ("herdr", "workspace", "list"): WORKSPACE_LIST.read_text(),
        ("herdr", "worktree", "list"): WORKTREE_LIST_ORPHAN.read_text(),
    }


#: What every pre-existing test in this file assumes `RealHerdr` does when
#: nobody has told it otherwise: one agent kind, launched with the historical
#: default args, on the one role these fixtures ever start. `_real`'s
#: `agents`/`routing` defaults exist ONLY to keep those tests' behaviour
#: unchanged now that `RealHerdr` no longer hardcodes `agent_kind`/`agent_args`
#: itself -- a test that cares about routing passes its own.
_DEFAULT_AGENTS = {
    "claude": AgentSpec(
        kind="claude", model_flag="--model", args=("--permission-mode", "acceptEdits")
    ),
}
_DEFAULT_ROUTING = {"implementer": ("claude", "claude-opus-5")}


def _real(tmp_path, run=None, worktree_root=None, agents=None, routing=None):
    from fleet.herdr import RealHerdr

    herdr = RealHerdr(
        worktree_root=worktree_root or tmp_path / "worktrees",
        projects_dir=tmp_path / "projects",
        prices={},
        repo_root=tmp_path / "repo",
        agents=_DEFAULT_AGENTS if agents is None else agents,
        routing=_DEFAULT_ROUTING if routing is None else routing,
    )
    herdr._run = run if run is not None else RecordingRun(_captured_replies())
    return herdr


def test_real_create_worktree_fetches_the_base_before_branching(tmp_path):
    """Parent §8.2's fresh-base rule is a fetch, not a sentence. Branching off a
    stale local `integration` is exactly the staleness the branch exists to
    remove."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    fetches = [c for c in run.calls if c[0] == "git" and "fetch" in c]
    assert fetches, f"no fetch before branching: {run.calls}"
    creates = run.herdr_calls("herdr", "worktree", "create")
    assert creates, f"no worktree create: {run.calls}"
    assert run.calls.index(fetches[0]) < run.calls.index(creates[0])


def test_real_create_worktree_names_the_branch_and_the_path(tmp_path):
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    path = herdr.create_worktree(task=1, slug="lid", base="integration")
    assert path == tmp_path / "worktrees" / "1-lid"
    create = run.herdr_calls("herdr", "worktree", "create")[0]
    assert create[create.index("--branch") + 1] == "fleet/1-lid"
    assert create[create.index("--path") + 1] == str(path)
    assert create[create.index("--label") + 1] == "fleet-1"


def test_real_create_worktree_bases_the_branch_on_the_fetched_remote_ref(tmp_path):
    """`--base integration` would branch off the stale local ref and make the
    fetch decorative -- the fetch updates `origin/integration`, not
    `integration`, so only the remote-tracking name carries what was fetched.
    Live: `herdr worktree create ... --base origin/integration` succeeded and
    produced a worktree tracking `origin/integration`."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    create = run.herdr_calls("herdr", "worktree", "create")[0]
    assert create[create.index("--base") + 1] == "origin/integration"


def test_real_create_worktree_asks_for_json_and_records_the_workspace_id(tmp_path):
    """The workspace id is the only handle `worktree remove` accepts, and the
    only place herdr offers it at creation time is the `--json` body. Without
    `--json` the id is never learned and every later removal falls back to a
    list scan."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=CAPTURE_TASK, slug="lid-small-sample", base="integration")
    create = run.herdr_calls("herdr", "worktree", "create")[0]
    assert "--json" in create
    assert herdr.workspaces[CAPTURE_TASK] == CAPTURE_WORKSPACE
    assert herdr.panes[CAPTURE_TASK] == CAPTURE_PANE


def test_real_create_worktree_does_not_steal_focus(tmp_path):
    """Six unattended dispatches that each yank the user's terminal to a new
    workspace is a tool fighting its operator."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    create = run.herdr_calls("herdr", "worktree", "create")[0]
    assert "--no-focus" in create
    assert "--focus" not in create


def _for_each_ref(tmp_path):
    """The argv prefix `_existing_branch` builds. Derived from the same tmp_path
    the client is built with, so a repo-root change cannot leave this keyed on a
    path nothing calls and silently answer "no branch" for every test."""
    return ("git", "-C", str(tmp_path / "repo"), "for-each-ref")


def test_real_create_worktree_looks_for_any_branch_of_this_task(tmp_path):
    """By the `fleet/<n>-` prefix, not by the slug of the title as it reads
    right now. `refs/heads/...` and not a bare name for the same reason a ref
    was used before: a TAG called `fleet/1-lid` must not answer for a branch."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    checks = run.herdr_calls(*_for_each_ref(tmp_path))
    assert checks, f"no branch check: {run.calls}"
    assert "refs/heads/fleet/1-*" in checks[0]


def test_real_create_worktree_reuses_a_branch_that_already_exists(tmp_path):
    """A re-dispatch after a rebase handback, and every continuation: the branch
    carries the work of the stage before it -- the commits the integrator asked
    to have re-based, or the implementation the reviewer was started to read.
    Recreating it from the base would discard exactly those commits, silently,
    and the next stage would begin from an empty tree."""
    replies = _captured_replies()
    replies[_for_each_ref(tmp_path)] = "fleet/1-lid\n"
    run = RecordingRun(replies)
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    create = run.herdr_calls("herdr", "worktree", "create")[0]
    assert create[create.index("--branch") + 1] == "fleet/1-lid"
    assert "--base" not in create


def test_real_create_worktree_reuses_the_branch_after_the_issue_is_retitled(tmp_path):
    """An issue retitled mid-flight -- a human sharpening it, or `fleet
    discover` rewriting it -- changes what `slug_of` returns. Looked up by
    exact slug, the branch carrying the implementer's commits reads as missing,
    and the create takes the `--base origin/<base>` path: the reviewer starts
    from an empty tree and the work is on a branch nothing checks out again.
    The task number is the identity; the slug is decoration."""
    replies = _captured_replies()
    replies[_for_each_ref(tmp_path)] = "fleet/1-lid-on-small-samples\n"
    run = RecordingRun(replies)
    herdr = _real(tmp_path, run)
    path = herdr.create_worktree(task=1, slug="clamp-neighbour-count", base="integration")
    create = run.herdr_calls("herdr", "worktree", "create")[0]
    assert create[create.index("--branch") + 1] == "fleet/1-lid-on-small-samples"
    assert "--base" not in create
    # The checkout follows the branch it is on, so the pair cannot drift apart.
    assert path == tmp_path / "worktrees" / "1-lid-on-small-samples"
    assert create[create.index("--path") + 1] == str(path)


def test_real_create_worktree_takes_the_most_recent_of_several_branches(tmp_path):
    """Two retitles leave two branches for one task. Whichever is picked must be
    the same one every tick -- alphabetical order would flip the reviewer onto
    the abandoned branch on a rename -- and it must be the one carrying the
    work, which is the one most recently committed to."""
    replies = _captured_replies()
    replies[_for_each_ref(tmp_path)] = "fleet/1-newest\nfleet/1-older\n"
    run = RecordingRun(replies)
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="whatever", base="integration")
    assert "--sort=-committerdate" in run.herdr_calls(*_for_each_ref(tmp_path))[0]
    create = run.herdr_calls("herdr", "worktree", "create")[0]
    assert create[create.index("--branch") + 1] == "fleet/1-newest"


def test_real_create_worktree_does_not_mistake_another_tasks_branch_for_its_own(tmp_path):
    """`fleet/1-*` must not match `fleet/12-lid`. It does not -- the dash is
    literal -- and this pins it, because task 1 inheriting task 12's branch
    would put two tasks' commits on one branch."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    pattern = run.herdr_calls(*_for_each_ref(tmp_path))[0][-1]
    assert pattern == "refs/heads/fleet/1-*"


def test_real_create_worktree_bases_a_branch_that_does_not_exist_yet(tmp_path):
    """`for-each-ref` exits 0 and prints nothing when the pattern matches no
    ref, so an empty answer is a missing branch and not a present one. Reading
    it the other way would make every FIRST dispatch reuse a branch that is not
    there, which herdr refuses -- no task would ever start."""
    run = RecordingRun(_captured_replies())  # nothing keyed on for-each-ref: ""
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    create = run.herdr_calls("herdr", "worktree", "create")[0]
    assert create[create.index("--base") + 1] == "origin/integration"


def test_real_create_worktree_bases_the_branch_when_git_cannot_be_asked(tmp_path):
    """A `for-each-ref` that fails outright says nothing about the branch. The
    safe reading is "not there": a first dispatch then works, and the only cost
    on a re-dispatch is a create herdr refuses -- loudly, one task. Read the
    other way, a failing git would silently drop every task's base."""

    class FailingLookup(RecordingRun):
        def __call__(self, *args):
            if "for-each-ref" in args:
                self.calls.append(args)
                raise HerdrError("for-each-ref failed: not a git repository")
            return super().__call__(*args)

    run = FailingLookup(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    create = run.herdr_calls("herdr", "worktree", "create")[0]
    assert create[create.index("--base") + 1] == "origin/integration"


def test_real_remove_worktree_addresses_the_workspace_not_the_path(tmp_path):
    """`herdr worktree remove <path>` does not exist. Run live it returns
    `unknown option: /some/path` and exit 2; the real command takes
    `--workspace <ID>`."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    path = herdr.create_worktree(task=CAPTURE_TASK, slug="lid-small-sample", base="integration")
    run.calls.clear()
    herdr.remove_worktree(task=CAPTURE_TASK)
    remove = run.herdr_calls("herdr", "worktree", "remove")[0]
    assert remove[remove.index("--workspace") + 1] == CAPTURE_WORKSPACE
    assert str(path) not in remove


def test_real_remove_worktree_forces(tmp_path):
    """A retired agent's worktree is very often dirty -- that is what being
    retired mid-turn means. Live, removing a dirty worktree without `--force`
    returns `dirty_worktree_requires_force`, so without the flag REAP would
    fail on exactly the worktrees it exists to clean up and wedge the slot."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=CAPTURE_TASK, slug="lid-small-sample", base="integration")
    herdr.remove_worktree(task=CAPTURE_TASK)
    assert "--force" in run.herdr_calls("herdr", "worktree", "remove")[0]


def test_real_remove_worktree_finds_a_workspace_it_did_not_create(tmp_path):
    """After a daemon restart the id-to-task map in memory is gone but the
    worktree is not. The id is recovered from `herdr workspace list`, matched
    by the checkout path -- the same path-is-identity rule the module header
    states, and the reason a restart is cheap.

    The capture this reads back is a real 11-workspace listing: most entries
    carry no `worktree` key at all, one is the *source* repo's own workspace
    (created as a side effect of `worktree create --cwd`) whose checkout is
    outside the worktree root, and only one is the fleet worktree. A lookup
    that indexed `workspace["worktree"]` unconditionally, or that matched on
    the label, would pick the wrong one."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run, worktree_root=CAPTURE_ROOT)
    assert herdr.workspaces == {}, "precondition: nothing learned from a create"
    herdr.remove_worktree(task=CAPTURE_TASK)
    remove = run.herdr_calls("herdr", "worktree", "remove")[0]
    assert remove[remove.index("--workspace") + 1] == CAPTURE_WORKSPACE


def test_real_remove_worktree_of_a_task_with_nothing_on_disk_removes_nothing(tmp_path):
    """Nothing to remove is a silent, successful no-op -- and it has to be
    distinguished from the leak below rather than collapsed with it. Task 4242
    appears in neither capture: no workspace, and no checkout at the git level
    either. Guessing an id here would remove some other workspace's checkout,
    and someone's uncommitted work with it."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run, worktree_root=ORPHAN_ROOT)
    herdr.remove_worktree(task=4242)
    assert run.herdr_calls("herdr", "worktree", "remove") == []


def test_real_remove_worktree_clears_a_checkout_that_has_no_workspace(tmp_path):
    """A checkout that exists with no workspace to remove it through is a leak,
    and reporting it is not clearing it: the checkout stays, the task returns to
    READY, and the next dispatch fails on the occupied path -- every tick,
    forever, with no counter moving.

    So the git-level removal the error message used to tell a human to run is
    run here instead. It is local, reversible, and confined to fleet's own
    worktree root: `_checkout_of` resolves the path through `task_of`, which
    rejects anything outside it.

    The capture is that exact state, produced live: create the worktree, then
    close its workspace. `herdr workspace list` cannot see the checkout
    afterwards (it is not a workspace any more), so `_workspace_of` answers
    None -- indistinguishable, without the git-level view, from the task above
    that has nothing anywhere."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run, worktree_root=ORPHAN_ROOT)
    herdr.remove_worktree(task=CAPTURE_TASK)
    removals = [c for c in run.calls if c[0] == "git" and "worktree" in c and "remove" in c]
    assert removals, f"the leaked checkout was reported, not removed: {run.calls}"
    assert removals[0][:3] == ("git", "-C", str(tmp_path / "repo"))
    assert "--force" in removals[0]
    assert removals[0][-1] == str(ORPHAN_ROOT / "12-lid-small-sample")
    assert run.herdr_calls("herdr", "worktree", "remove") == [], (
        "there is no workspace to address; `herdr worktree remove` needs one"
    )


def test_real_remove_worktree_raises_when_even_git_cannot_clear_the_checkout(tmp_path):
    """The fallback is a best effort, not a guarantee -- a checkout on a
    filesystem that is gone, or a git that is not there. When it fails there is
    nothing left to try, and the task's next dispatch will fail on the occupied
    path, so the message has to name the path and the command a human runs.
    Silence here would make the next failure point at the wrong thing."""

    class FailingRemoval(RecordingRun):
        def __call__(self, *args):
            if "worktree" in args and "remove" in args and args[0] == "git":
                self.calls.append(args)
                raise HerdrError("git worktree remove failed: not a working tree")
            return super().__call__(*args)

    run = FailingRemoval(_captured_replies())
    herdr = _real(tmp_path, run, worktree_root=ORPHAN_ROOT)
    with pytest.raises(HerdrError) as excinfo:
        herdr.remove_worktree(task=CAPTURE_TASK)
    message = str(excinfo.value)
    assert str(CAPTURE_TASK) in message
    assert "12-lid-small-sample" in message


# --- error codes -----------------------------------------------------------
#
# herdr answers a failure with `{"error":{"code":...,"message":...}}` on stderr
# and exit 1 (fixtures/herdr/README.md records the session). The code is the
# only thing that tells one failure from another: `_subprocess` otherwise
# collapses every non-zero exit into one HerdrError, and a caller deciding
# whether it is safe to delete a worktree cannot tell "the agent was already
# gone" from "herdr rejected the key syntax and the agent is still running".


def test_a_herdr_error_code_is_recovered_from_the_json_body():
    assert error_code('{"error":{"code":"agent_not_found","message":"agent target x"}}') == (
        "agent_not_found"
    )
    assert error_code('{"error":{"code":"invalid_key","message":"unsupported key ctrl-c"}}') == (
        "invalid_key"
    )


def test_a_plain_text_herdr_failure_has_no_code():
    """Not every failure is JSON: `--cwd` on a command that has no such option
    answers `unknown option: --cwd` and exit 2. A parser that raised here would
    replace a diagnosable error with a JSONDecodeError, and one that invented a
    code would let a caller act on a failure it has not actually recognised."""
    assert error_code("unknown option: --cwd") is None
    assert error_code("") is None


@pytest.mark.parametrize(
    "body",
    [
        '{"error": "boom"}',  # `error` present, but a string rather than an object
        '{"error": null}',
        '{"error": ["boom"]}',
        '{"error": {"message": "no code in here"}}',
        '{"code": "agent_not_found"}',  # a code, but not under `error`
        "[]",
        '"just a string"',
        "123",
        "null",
    ],
    ids=lambda b: b,
)
def test_json_that_is_not_herdrs_error_shape_yields_no_code(body):
    """Every caller of this reads the answer inside an `except HerdrError`, and
    the raise that builds that HerdrError is where this is called from. So a
    shape this does not recognise has to answer None, not raise: an exception
    here escapes `_subprocess`'s own handler, no HerdrError is raised at all,
    and an AttributeError sails past every `except HerdrError` in the executor
    -- taking down the whole tick, which is worse than the wrong-but-caught
    decision the code exists to prevent.

    None is also the safe answer on its own terms: no code means the cautious
    path, which leaves a worktree alone rather than deleting it under an agent
    that may still be running."""
    assert error_code(body) is None


def test_a_code_that_is_not_a_string_is_not_a_code():
    """`str(code)` on whatever turns up would hand the executor `{'a': 1}` as a
    code to compare against AGENT_ABSENT_CODES -- never equal, so harmless
    today, but it is a value the vocabulary has no meaning for and it would
    read as a real code in the runlog."""
    assert error_code('{"error": {"code": 42}}') is None
    assert error_code('{"error": {"code": {"nested": true}}}') is None
    assert error_code('{"error": {"code": ""}}') is None


def test_a_code_is_read_from_the_last_line_of_a_noisy_stderr():
    """Warnings on stderr before the body are ordinary. Parsing only the whole
    buffer would drop the code the moment anything else printed first."""
    assert error_code('warning: something\n{"error":{"code":"agent_not_found"}}') == (
        "agent_not_found"
    )


def test_a_real_subprocess_failure_carries_the_code_herdr_returned(tmp_path):
    """Through the real `subprocess` path, not a double: `_subprocess` is where
    stderr is captured, and a code parsed from a string the process never
    actually emitted would prove nothing about where it lands."""
    herdr = _real(tmp_path, run=None)
    body = '{"error":{"code":"agent_not_found","message":"agent target fleet-1 not found"}}'
    with pytest.raises(HerdrError) as excinfo:
        herdr._subprocess("sh", "-c", f"printf '%s' '{body}' >&2; exit 1")
    assert excinfo.value.code == "agent_not_found"
    assert "agent target fleet-1 not found" in str(excinfo.value)


def test_the_fake_names_the_same_code_the_real_client_returns():
    """The executor decides whether to delete a worktree on this code. A fake
    that raised without one would make that decision untestable offline and
    would send the executor down the cautious path for every stop -- leaking a
    worktree per task, silently."""
    herdr = FakeHerdr()
    with pytest.raises(HerdrError) as excinfo:
        herdr.stop_agent(task=1)
    assert excinfo.value.code == "agent_not_found"


def test_an_error_raised_without_a_code_still_has_the_attribute():
    """Every `except HerdrError` in the daemon reads `.code`. An attribute that
    exists on some instances and not others is an AttributeError inside an
    exception handler, which is the worst place to find one."""
    assert HerdrError("something went wrong").code is None


def test_real_remove_worktree_does_not_consult_git_when_it_has_a_workspace(tmp_path):
    """The leak check is on the None branch only. Listing worktrees on every
    successful removal would be a second subprocess per reap for nothing."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=CAPTURE_TASK, slug="lid-small-sample", base="integration")
    run.calls.clear()
    herdr.remove_worktree(task=CAPTURE_TASK)
    assert run.herdr_calls("herdr", "worktree", "list") == []


def test_real_remove_worktree_asks_git_from_the_repo_root(tmp_path):
    """`worktree list` is scoped to a repo. Without `--cwd <repo_root>` it
    answers for whatever repo the daemon's own cwd happens to be in, which is
    not necessarily the one holding fleet's worktrees."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run, worktree_root=ORPHAN_ROOT)
    herdr.remove_worktree(task=4242)
    listed = run.herdr_calls("herdr", "worktree", "list")[0]
    assert listed[listed.index("--cwd") + 1] == str(tmp_path / "repo")
    assert "--json" in listed


def test_real_create_worktree_raises_when_herdr_returns_no_workspace_id(tmp_path):
    """A create that cached no workspace id is not a successful create, and it
    must not look like one. Returning the path regardless -- which a truthiness
    guard on the id does -- leaves a checkout on disk that nothing can remove,
    and the caller with no way to know. A herdr field rename is exactly how
    this happens."""
    replies = _captured_replies()
    renamed = json.loads(WORKTREE_CREATED.read_text())
    renamed["result"]["workspace"]["id"] = renamed["result"]["workspace"].pop("workspace_id")
    replies[("herdr", "worktree", "create")] = json.dumps(renamed)
    run = RecordingRun(replies)
    herdr = _real(tmp_path, run)
    with pytest.raises(HerdrError) as excinfo:
        herdr.create_worktree(task=1, slug="lid", base="integration")
    assert "no workspace id" in str(excinfo.value)


def test_real_start_agent_exports_the_role_and_the_agent_id(tmp_path):
    """The runbook is explicit that an unset FLEET_AGENT attributes packets to
    'manual', which is indistinguishable from a human at a keyboard -- so an
    unattended run that produced it would look exactly like correct operation."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    herdr.start_agent(task=1, role="implementer", prompt="do the thing")
    assert "FLEET_ROLE=implementer" in run.flat
    assert "FLEET_AGENT=fleet-1" in run.flat


def test_real_start_agent_exports_through_the_pane_not_the_agent(tmp_path):
    """`herdr agent send-keys` addresses a *registered agent*. Before
    `agent start` there is no agent, and the live CLI answers
    `{"code":"agent_not_found"}` -- so an export sent that way never reaches the
    shell, and every packet the agent writes is attributed to 'manual'. The
    pane family is the one that works before an agent exists."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    herdr.start_agent(task=1, role="implementer", prompt="x")
    exports = [
        c for c in run.calls if any("FLEET_ROLE=implementer FLEET_AGENT=fleet-1" in a for a in c)
    ]
    assert exports, f"no export sent: {run.calls}"
    assert exports[0][:3] == ("herdr", "pane", "send-text")


def test_real_start_agent_commits_the_export_with_an_enter_key(tmp_path):
    """`pane send-text` types; it does not submit. Without the Enter the export
    sits unexecuted on the command line and is then swallowed by the agent that
    starts on top of it -- and FLEET_AGENT is silently unset. Verified live:
    send-text then `send-keys Enter`, then `echo $FLEET_AGENT` in the same pane
    printed `fleet-999`."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    herdr.start_agent(task=1, role="implementer", prompt="x")
    text = [i for i, c in enumerate(run.calls) if c[:3] == ("herdr", "pane", "send-text")][0]
    enters = [
        i
        for i, c in enumerate(run.calls)
        if c[:3] == ("herdr", "pane", "send-keys") and c[-1] == "Enter"
    ]
    assert enters, f"the export was never submitted: {run.calls}"
    assert text < enters[0]
    starts = [i for i, c in enumerate(run.calls) if c[:3] == ("herdr", "agent", "start")][0]
    assert enters[0] < starts


def test_real_start_agent_resolves_the_pane_by_workspace_id(tmp_path):
    """`herdr pane list --cwd <path>` is not a thing: run live it returns
    `unknown option: --cwd` and exit 2. Panes are listed by workspace."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run, worktree_root=CAPTURE_ROOT)
    herdr.start_agent(task=CAPTURE_TASK, role="implementer", prompt="x")
    listed = run.herdr_calls("herdr", "pane", "list")[0]
    assert listed[listed.index("--workspace") + 1] == CAPTURE_WORKSPACE
    assert "--cwd" not in listed
    start = run.herdr_calls("herdr", "agent", "start")[0]
    assert start[start.index("--pane") + 1] == CAPTURE_PANE


def test_real_start_agent_grants_permissions_up_front(tmp_path):
    """A weak test, kept honest about it. **This does not show that the agent
    reaches a usable prompt**, and it must not be read as the trust-prompt
    question being settled.

    The measurement that produced fixtures/herdr/agent-fleet-worktree.json
    disproves the obvious reading: the default `--permission-mode acceptEdits`
    satisfies the assertion below, and the agent still came up `agent_status:
    blocked`, sitting at claude's trust-this-folder dialog -- which every fresh
    worktree gets, because trust is keyed per directory path. `STATUS` maps
    `blocked` to `dirty`, so under this default a dispatch reads as dirty on
    its first tick.

    Whether `--dangerously-skip-permissions` clears it is UNMEASURED: the
    sandbox classifier blocked that command, and it is not assumed here. The
    default is a human's decision and is escalated, not settled.

    What this actually discriminates -- that some permission argument reaches
    the argv -- is already covered exactly by
    `test_real_start_agent_passes_agent_args_after_a_separator`, which pins the
    tuple after the `--`. This one is left as the place the open question is
    written down."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    herdr.start_agent(task=1, role="implementer", prompt="x")
    assert "--permission-mode" in run.flat or "--dangerously-skip-permissions" in run.flat


def test_real_start_agent_passes_agent_args_after_a_separator(tmp_path):
    """`herdr agent start <NAME> --kind K --pane P [-- [AGENT_ARG]...]`: without
    the `--`, `--permission-mode` is parsed as one of herdr's own options and
    the start fails instead of reaching claude.

    Pins the whole post-separator sequence, not just `spec.args`: since task
    7 the model always lands after `spec.args` on that same side of `--` (see
    `test_a_role_is_started_on_its_declared_agent_and_model`), so a tuple
    that ended exactly at `acceptEdits` would now be wrong by construction --
    but a prefix check would also miss the model flag being appended twice,
    junk appended after it, or the model landing ahead of `spec.args` instead
    of after it. `_DEFAULT_AGENTS["claude"].args` and
    `_DEFAULT_ROUTING["implementer"]` are both fixed in this file, so the
    full tuple is exactly determined and worth pinning exactly."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    herdr.start_agent(task=1, role="implementer", prompt="x")
    start = run.herdr_calls("herdr", "agent", "start")[0]
    assert start[start.index("--") + 1 :] == (
        "--permission-mode",
        "acceptEdits",
        "--model",
        "claude-opus-5",
    )
    assert start[3] == "fleet-1"
    assert start[start.index("--kind") + 1] == "claude"


def test_real_start_agent_sends_the_prompt_after_the_agent_is_up(tmp_path):
    """`herdr agent start` requires a pane already at an interactive shell
    prompt, and `agent prompt` requires an agent already detected. Reversed, the
    prompt is typed into a shell."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    herdr.start_agent(task=1, role="implementer", prompt="do the thing")
    kinds = [c[2] for c in run.herdr_calls("herdr", "agent")]
    assert kinds.index("start") < kinds.index("prompt")
    prompt = run.herdr_calls("herdr", "agent", "prompt")[0]
    assert prompt[3:] == ("fleet-1", "do the thing")


def test_real_stop_agent_uses_a_key_name_herdr_accepts(tmp_path):
    """herdr's send-keys takes *named* keys, not escape sequences. Live,
    `send-keys <pane> '\\x03'` returns `{"code":"invalid_key","message":
    "unsupported key \\x03"}` and exit 1, and so does `ctrl-c`; `C-c` returns
    ok and put `^C` in the pane. A literal escape string here would make
    stop_agent a no-op that reports success -- the agent keeps burning budget
    while REAP believes it stopped."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.stop_agent(task=1)
    keys = run.herdr_calls("herdr", "agent", "send-keys")[0]
    assert keys[3] == "fleet-1"
    assert set(keys[4:]) == {"C-c"}
    assert "\x03" not in run.flat and "\\x03" not in run.flat


def test_real_stop_agent_sends_the_interrupt_twice(tmp_path):
    """Measured, not assumed: one `C-c` into a live `claude` left it running
    (still in `herdr agent list`); a second in the same call exited it and the
    agent disappeared from the list. A single interrupt would leave a running
    agent that REAP has already written off -- a lost slot and unbounded
    spend."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.stop_agent(task=1)
    keys = run.herdr_calls("herdr", "agent", "send-keys")[0]
    assert keys[4:] == ("C-c", "C-c")


def test_real_stop_agent_and_remove_worktree_are_separate(tmp_path):
    """A retired agent's worktree is removed, but a released one's branch has
    the work on it. Fusing the two would delete a task's commits on a stall."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run)
    herdr.create_worktree(task=1, slug="lid", base="integration")
    run.calls.clear()
    herdr.stop_agent(task=1)
    assert "worktree" not in run.flat


def test_real_agents_parses_a_captured_snapshot(tmp_path):
    """Wiring test for `agents()`: the snapshot call is `herdr api snapshot`
    and its body goes through `parse_snapshot` with this herdr's own root."""
    run = RecordingRun({("herdr", "api", "snapshot"): AGENT_IN_WORKTREE.read_text()})
    herdr = _real(tmp_path, run, worktree_root=CAPTURE_ROOT)
    states = herdr.agents()
    assert run.calls == [("herdr", "api", "snapshot")]
    assert [a.task for a in states] == [999]


def test_a_real_fleet_agent_reports_its_worktree_in_cwd():
    """Task 10's open gate item, closed by measurement.

    This is a verbatim `herdr api snapshot` record for an agent started by
    `herdr agent start fleet-999 --kind claude --pane w3Q:p1 --
    --permission-mode acceptEdits` inside a real `herdr worktree create`
    worktree. Task 10 could only show a *different* tool's nested worktree
    reporting the main checkout in `cwd`, which left open the possibility that
    a fleet agent would too -- and a cwd-only read would then have dropped
    every live fleet agent silently. It does not: `cwd` carries the worktree.
    Pinned here so a herdr change that moves it fails a test instead of
    emptying `agents()`."""
    raw = json.loads(AGENT_IN_WORKTREE.read_text())
    (agent,) = raw["result"]["snapshot"]["agents"]
    assert agent["cwd"] == str(CAPTURE_ROOT / "999-probe")
    assert agent["cwd"] == agent["foreground_cwd"]
    assert parse_snapshot(raw, CAPTURE_ROOT) == [
        AgentState(task=999, status="dirty", change_seq=agent["state_change_seq"], cost_usd=0.0)
    ]


def test_a_freshly_started_agent_has_no_session_id_yet():
    """The same capture, one field further: a just-started agent record carries
    no `agent_session` key at all. `parse_snapshot` must read it as cost 0.0
    rather than raising -- a KeyError on the first tick after every dispatch
    would take down the whole tick, not one task."""
    raw = json.loads(AGENT_IN_WORKTREE.read_text())
    (agent,) = raw["result"]["snapshot"]["agents"]
    assert "agent_session" not in agent
    prices = {"m": {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0}}
    providers = {"claude": ClaudeProvider(projects_dir=Path("/nonexistent"))}
    parsed = parse_snapshot(raw, CAPTURE_ROOT, providers=providers, prices=prices)
    assert parsed[0].cost_usd == 0.0


def test_real_run_raises_herdr_error_carrying_stderr(tmp_path):
    """CalledProcessError carries argv and an exit code but not stderr, which is
    where herdr puts the reason -- `{"code":"dirty_worktree_requires_force"}`
    and friends. Losing it turns every failure into 'exit 1'."""
    herdr = _real(tmp_path)
    with pytest.raises(HerdrError) as excinfo:
        herdr._subprocess("git", "--this-flag-does-not-exist")
    assert "--this-flag-does-not-exist" in str(excinfo.value)
    assert "unknown option" in str(excinfo.value)


def test_real_run_raises_herdr_error_when_output_is_not_json(tmp_path):
    """herdr prints its errors as JSON on stdout for some failures and plain
    text for others (`unknown option: --cwd`, exit 2). A bare json.JSONDecodeError
    out of the daemon tick names neither the command nor the output."""
    run = RecordingRun({("herdr",): "unknown option: --cwd"})
    herdr = _real(tmp_path, run)
    with pytest.raises(HerdrError) as excinfo:
        herdr.agents()
    assert "unknown option" in str(excinfo.value)


def _unpriced_transcript(tmp_path, session, model="unpriced-model"):
    project = tmp_path / "projects" / "slug"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session}.jsonl").write_text(
        json.dumps({"message": {"model": model, "usage": {"input_tokens": 1}}}) + "\n"
    )
    # A non-empty table that simply lacks the model -- an empty dict is falsy
    # and skips the join branch entirely, proving nothing.
    return {"other-model": {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0}}


def test_an_unpriced_model_is_recorded_rather_than_raised_when_someone_is_collecting(tmp_path):
    """The daemon's half of the same fact. Raising out of `parse_snapshot` takes
    down the WHOLE tick -- REAP included -- when the correct response is to
    drain: in-flight agents finish and nothing new starts. With a collector the
    read completes, every other agent still reports, and the collector is what
    turns into the drain."""
    session = "33333333-3333-3333-3333-333333333333"
    prices = _unpriced_transcript(tmp_path, session)
    agent = _agent(str(ROOT / "5-slug"), agent_session={"value": session})
    providers = {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}
    unpriced: set[str] = set()
    parsed = parse_snapshot(
        _snapshot([agent]),
        ROOT,
        providers=providers,
        prices=prices,
        unpriced=unpriced,
    )
    assert [a.task for a in parsed] == [5]
    assert parsed[0].cost_usd == 0.0
    assert any("unpriced-model" in message for message in unpriced)


def test_an_unpriced_agent_does_not_silence_the_rest_of_the_snapshot(tmp_path):
    """The reason the exception had to stop crossing the seam: one agent on a
    model nobody priced must not erase five agents that priced fine."""
    session = "44444444-4444-4444-4444-444444444444"
    prices = _unpriced_transcript(tmp_path, session)
    agents = [
        _agent(str(ROOT / "5-slug"), agent_session={"value": session}),
        _agent(str(ROOT / "6-other"), agent_session={"value": "no-transcript-at-all"}),
    ]
    providers = {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}
    parsed = parse_snapshot(
        _snapshot(agents), ROOT, providers=providers, prices=prices, unpriced=set()
    )
    assert [a.task for a in parsed] == [5, 6]


def test_a_fake_herdr_reports_no_unpriced_models_by_default(tmp_path):
    """Empty means "every model priced", which is what a fake with no seeded
    price problem should say. A fake that reported one would drain every test."""
    assert FakeHerdr().unpriced() == set()


def test_a_fake_herdr_reports_the_models_a_test_seeds(tmp_path):
    herdr = FakeHerdr()
    herdr.unpriced_models = {"no price for model 'claude-new'"}
    assert herdr.unpriced() == {"no price for model 'claude-new'"}


def test_a_fake_herdr_hands_out_a_copy(tmp_path):
    """The daemon reads this every tick. Handing out the live set lets a caller
    clear the one fact that stops the fleet spending."""
    herdr = FakeHerdr()
    herdr.unpriced_models = {"m"}
    herdr.unpriced().clear()
    assert herdr.unpriced() == {"m"}


def test_real_unpriced_is_empty_before_the_first_read(tmp_path):
    """A daemon constructed and not yet ticked has learned nothing about
    prices. Reporting an unpriced model there would drain tick 1 of every run."""
    assert _real(tmp_path).unpriced() == set()


def test_real_unpriced_is_rebuilt_by_every_read(tmp_path):
    """Fresh per call, not accumulated. Accumulated, it grows all night and a
    model priced at 02:05 keeps the fleet drained until dawn -- and the set
    itself is then something that only ever gets bigger."""
    session = "55555555-5555-5555-5555-555555555555"
    prices = _unpriced_transcript(tmp_path, session)
    root = tmp_path / "worktrees"
    agent = _agent(str(root / "5-slug"), agent_session={"value": session})
    run = RecordingRun({("herdr", "api", "snapshot"): json.dumps(_snapshot([agent]))})
    herdr = _real(tmp_path, run, worktree_root=root)
    herdr.prices = prices
    herdr.projects_dir = tmp_path / "projects"
    herdr.agents()
    assert any("unpriced-model" in message for message in herdr.unpriced())

    run.replies[("herdr", "api", "snapshot")] = json.dumps(_snapshot([]))
    herdr.agents()
    assert herdr.unpriced() == set()


def test_both_clients_answer_unpriced(tmp_path):
    """The Protocol is what the daemon codes against, and a method on only one
    of the two is a fake/real divergence of exactly the class m2-inputs §6
    names."""
    from fleet.herdr import Herdr, RealHerdr

    for cls in (FakeHerdr, RealHerdr, Herdr):
        assert callable(getattr(cls, "unpriced", None)), cls


def test_an_empty_price_table_makes_every_priceable_agent_unpriced(tmp_path):
    """The shape spec §4.5 exists to prevent, arriving through the door nobody
    wrote a case for: not an unknown model, but no `[prices]` table at all.

    `if ... and prices:` treated an empty dict as "do not price", so every
    agent read as `cost_usd = 0.0`, `unpriced()` stayed empty, the daemon never
    drained, and `per_task_usd` could never fire either -- a whole night with
    the budget ceiling switched off and nothing to say so. An empty table
    prices nothing, which is exactly the condition `UnpricedModel` names, so it
    must arm the drain the same way an unknown model does.
    """
    session = "33333333-3333-3333-3333-333333333333"
    project = tmp_path / "projects" / "slug"
    project.mkdir(parents=True)
    (project / f"{session}.jsonl").write_text(
        json.dumps({"message": {"model": "claude-opus-4", "usage": {"input_tokens": 1}}}) + "\n"
    )
    agent = _agent(str(ROOT / "5-slug"), agent_session={"value": session})
    providers = {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}
    unpriced: set[str] = set()
    parsed = parse_snapshot(
        _snapshot([agent]), ROOT, providers=providers, prices={}, unpriced=unpriced
    )
    assert unpriced, "an empty [prices] table priced an agent at nothing, silently"
    assert parsed[0].cost_usd == 0.0


def test_an_empty_price_table_raises_when_nobody_is_collecting(tmp_path):
    """Same rule, no collector: the exception propagates rather than being
    defaulted to zero. A caller that forgot the argument must not get a fleet of
    free agents just because the table happened to be empty rather than wrong."""
    session = "44444444-4444-4444-4444-444444444444"
    project = tmp_path / "projects" / "slug"
    project.mkdir(parents=True)
    (project / f"{session}.jsonl").write_text(
        json.dumps({"message": {"model": "claude-opus-4", "usage": {"input_tokens": 1}}}) + "\n"
    )
    agent = _agent(str(ROOT / "5-slug"), agent_session={"value": session})
    providers = {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}
    with pytest.raises(UnpricedModel):
        parse_snapshot(_snapshot([agent]), ROOT, providers=providers, prices={})


def test_real_create_worktree_names_the_push_prerequisite_when_the_base_is_not_on_the_remote(
    tmp_path,
):
    """`--base origin/<base>` needs `origin/<base>` to exist, and nothing in
    this project ever pushes: `git.py`'s whole seam is local, `integrator.md`
    has no push step, and fixtures/seed/reset.sh resets a LOCAL integration ref.
    So the fresh-base rule has a prerequisite -- the integrator must push
    `integration` -- and a fixture repo that has never had one pushed fails here
    with git's own `couldn't find remote ref integration`, at 2am, on a gate
    run. Naming the prerequisite is the difference between a five-minute fix and
    an hour reading herdr's source."""
    from fleet.herdr import HerdrError as _HerdrError

    def run(*args):
        if args[0] == "git" and "fetch" in args:
            raise _HerdrError("git fetch origin integration failed: couldn't find remote ref")
        return RecordingRun(_captured_replies())(*args)

    herdr = _real(tmp_path, run)
    with pytest.raises(HerdrError) as caught:
        herdr.create_worktree(task=1, slug="lid", base="integration")
    message = str(caught.value)
    assert "origin/integration" in message
    assert "push" in message
    assert "docs/m3-inputs.md" in message


def test_a_hung_herdr_call_times_out_rather_than_freezing_the_tick(tmp_path, monkeypatch):
    """No `subprocess.run` in the tree carried a timeout.

    `_subprocess` runs `herdr api snapshot` at the top of every tick and
    `git fetch origin <base>` on every dispatch. One hung fetch -- a network
    stall, a credential prompt on a tty that is not there -- blocks the tick
    loop for as long as the kernel will allow, while the state socket keeps
    serving the LAST snapshot from its own thread. So `fleet status` renders a
    healthy fleet all night and only the frozen `at` timestamp says otherwise.

    Through the real subprocess path, with the module's own timeout shortened,
    because the whole claim is about what `subprocess.run` does.
    """
    import fleet.herdr as herdr_module

    monkeypatch.setattr(herdr_module, "SUBPROCESS_TIMEOUT_SECONDS", 0.2)
    herdr = _real(tmp_path, run=None)
    with pytest.raises(HerdrError) as caught:
        herdr._subprocess("sh", "-c", "sleep 30")
    assert "timed out" in str(caught.value)


def test_a_timed_out_herdr_call_takes_the_cautious_path(tmp_path, monkeypatch):
    """No code, deliberately. `HerdrExecutor._stop_agent` removes a worktree
    only when the code says the agent is gone, and a call that timed out says
    nothing about the agent -- it may be running, mid-write, in that checkout.
    An invented `timeout` code would also be a name herdr may one day use for
    something else."""
    import fleet.herdr as herdr_module
    from fleet.herdr import AGENT_ABSENT_CODES

    monkeypatch.setattr(herdr_module, "SUBPROCESS_TIMEOUT_SECONDS", 0.2)
    herdr = _real(tmp_path, run=None)
    with pytest.raises(HerdrError) as caught:
        herdr._subprocess("sh", "-c", "sleep 30")
    assert caught.value.code is None
    assert caught.value.code not in AGENT_ABSENT_CODES


# -- launching each role on its declared agent and model ---------------------


def test_a_role_is_started_on_its_declared_agent_and_model(tmp_path):
    """Catches a launcher that keeps one global agent_kind, which would run
    every role on claude no matter what policy declares."""
    run = RecordingRun(_captured_replies())
    herdr = _real(
        tmp_path,
        run,
        agents={
            "codex": AgentSpec(kind="codex", model_flag="--model", args=("--full-auto",)),
        },
        routing={"implementer": ("codex", "gpt-5.6-sol")},
    )
    herdr.create_worktree(task=7, slug="lid", base="integration")
    herdr.start_agent(7, "implementer", "do the thing")
    start = run.herdr_calls("herdr", "agent", "start")[0]
    assert "--kind" in start and start[start.index("--kind") + 1] == "codex"
    assert "--model" in start and start[start.index("--model") + 1] == "gpt-5.6-sol"
    assert "--full-auto" in start


def test_a_role_with_no_declared_agent_raises_before_launching(tmp_path):
    """Catches falling back to a default kind, which would start an agent whose
    transcript the fleet cannot attribute to a declared model."""
    run = RecordingRun(_captured_replies())
    herdr = _real(tmp_path, run, agents={}, routing={})
    with pytest.raises(HerdrError, match="reviewer"):
        herdr.start_agent(7, "reviewer", "do the thing")
    assert not run.herdr_calls("herdr", "agent", "start")


# --------------------------------------------------------------------------
# Task 8: the model each agent actually ran, joined from its own transcript.


PRICED = {"test-model": {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0}}


def _claude_transcript(tmp_path, session, model="test-model", tokens=1_000):
    """A one-message Claude transcript, mirroring `_unpriced_transcript` above."""
    project = tmp_path / "projects" / "slug"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session}.jsonl").write_text(
        json.dumps({"message": {"model": model, "usage": {"input_tokens": tokens}}}) + "\n"
    )
    return {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}


def test_parse_snapshot_records_the_model_a_transcript_names(tmp_path):
    """decide compares this against the declared model. Catches leaving it None,
    which makes every mismatch check pass vacuously and lets a silent model
    substitution run the whole night unnoticed."""
    session = "55555555-5555-5555-5555-555555555555"
    providers = _claude_transcript(tmp_path, session)
    agent = _agent(str(ROOT / "5-slug"), agent="claude", agent_session={"value": session})
    parsed = parse_snapshot(_snapshot([agent]), ROOT, providers=providers, prices=PRICED)
    assert parsed[0].model == "test-model"


def test_a_model_carrying_no_tokens_is_not_recorded_as_the_observed_model(tmp_path):
    """`<synthetic>` has a usage block of all zeros. Catches recording it as the
    observed model, which reports a mismatch against every declared model."""
    session = "66666666-6666-6666-6666-666666666666"
    project = tmp_path / "projects" / "slug"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session}.jsonl").write_text(
        json.dumps({"message": {"model": "test-model", "usage": {"input_tokens": 1000}}})
        + "\n"
        + json.dumps({"message": {"model": "<synthetic>", "usage": {"input_tokens": 0}}})
        + "\n"
    )
    prices = dict(PRICED)
    prices["<synthetic>"] = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    providers = {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}
    agent = _agent(str(ROOT / "5-slug"), agent="claude", agent_session={"value": session})
    parsed = parse_snapshot(_snapshot([agent]), ROOT, providers=providers, prices=prices)
    assert parsed[0].model == "test-model"


def test_an_agent_on_an_unregistered_cli_is_collected_not_priced_at_zero(tmp_path):
    """Defence in depth behind the load-time check. Catches reading an unknown
    CLI's agent as free, which leaves the ceiling off for that agent while every
    other agent reports normally."""
    session = "77777777-7777-7777-7777-777777777777"
    providers = _claude_transcript(tmp_path, session)
    agent = _agent(str(ROOT / "7-slug"), agent="nosuchcli", agent_session={"value": session})
    unpriced: set[str] = set()
    parsed = parse_snapshot(
        _snapshot([agent]), ROOT, providers=providers, prices=PRICED, unpriced=unpriced
    )
    assert parsed[0].cost_usd == 0.0
    assert parsed[0].model is None
    assert any("nosuchcli" in message for message in unpriced)


def test_an_unregistered_cli_raises_when_nobody_is_collecting(tmp_path):
    """Mirrors the UnpricedModel rule directly above it: a caller that forgot
    the collector must not get a fleet of free agents."""
    session = "88888888-8888-8888-8888-888888888888"
    providers = _claude_transcript(tmp_path, session)
    agent = _agent(str(ROOT / "7-slug"), agent="nosuchcli", agent_session={"value": session})
    with pytest.raises(UnpricedModel, match="nosuchcli"):
        parse_snapshot(_snapshot([agent]), ROOT, providers=providers, prices=PRICED)


def test_a_transcript_naming_two_token_carrying_models_records_the_sentinel(tmp_path):
    """Catches collapsing a multi-model transcript into None: None is
    deliberately ignored by the later mismatch check, so a mid-session
    substitution -- exactly what that check exists to catch -- would pass it
    vacuously if this recorded None instead of MULTIPLE_MODELS."""
    session = "99999999-9999-9999-9999-999999999999"
    project = tmp_path / "projects" / "slug"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session}.jsonl").write_text(
        json.dumps({"message": {"model": "test-model", "usage": {"input_tokens": 1000}}})
        + "\n"
        + json.dumps({"message": {"model": "other-model", "usage": {"input_tokens": 500}}})
        + "\n"
    )
    prices = dict(PRICED)
    prices["other-model"] = {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0}
    providers = {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}
    agent = _agent(str(ROOT / "5-slug"), agent="claude", agent_session={"value": session})
    parsed = parse_snapshot(_snapshot([agent]), ROOT, providers=providers, prices=prices)
    assert parsed[0].model == MULTIPLE_MODELS


def test_an_agent_with_no_transcript_yet_leaves_model_unknown(tmp_path):
    """Catches recording any placeholder for 'not written yet' other than
    None. A transcript missing in the seconds after a dispatch is ordinary;
    the later mismatch check deliberately ignores None so it does not
    escalate on every fresh dispatch and burn the backlog on the first tick."""
    session = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    providers = {"claude": ClaudeProvider(projects_dir=tmp_path / "projects")}
    agent = _agent(str(ROOT / "5-slug"), agent="claude", agent_session={"value": session})
    parsed = parse_snapshot(_snapshot([agent]), ROOT, providers=providers, prices=PRICED)
    assert parsed[0].model is None
    assert parsed[0].cost_usd == 0.0
