from __future__ import annotations

import json
from pathlib import Path

from fleet.herdr import STATUS, FakeHerdr, task_of

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/herdr/snapshot-fleet.json"
ROOT = Path("/home/u/.fleet/worktrees")


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


def test_the_fake_yields_agents_for_fleet_worktrees_only(tmp_path):
    fake = FakeHerdr()
    fake.add_agent(task=3, status="working", change_seq=7, cost_usd=0.25)
    fake.add_agent(task=9, status="done", change_seq=8)
    assert [a.task for a in fake.agents()] == [3, 9]
    assert fake.agents()[0].status == "working"
    assert fake.agents()[0].cost_usd == 0.25


def test_the_captured_snapshot_parses():
    """The fidelity obligation: a parser that has only met hand-built dicts has
    not met herdr."""
    from fleet.herdr import parse_snapshot

    raw = json.loads(FIXTURE.read_text())
    parsed = parse_snapshot(raw, ROOT)
    assert isinstance(parsed, list)


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
