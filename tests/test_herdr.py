from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from fleet.cost import UnpricedModel
from fleet.herdr import STATUS, FakeHerdr, parse_snapshot, task_of

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
    parsed = parse_snapshot(
        _snapshot([agent]), ROOT, projects_dir=tmp_path / "projects", prices=prices
    )
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
    with pytest.raises(UnpricedModel):
        parse_snapshot(_snapshot([agent]), ROOT, projects_dir=tmp_path / "projects", prices=prices)


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
