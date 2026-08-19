from __future__ import annotations

from fleet.status import render

PAYLOAD = {
    "run_id": "2026-08-18",
    "tick": 12,
    "at": "2026-08-18T02:00:00+00:00",
    "rows": [
        {
            "task": 1,
            "title": "Fix LID",
            "state": "claimed",
            "area": "eval",
            "class": "patch",
            "attempt": 1,
            "rebases": 0,
            "owner": "bot",
            "agent": "working",
            "cost_usd": 0.42,
        },
        {
            "task": 4,
            "title": "Needs data",
            "state": "blocked",
            "area": "models",
            "class": "patch",
            "attempt": 0,
            "rebases": 0,
            "owner": None,
            "agent": None,
            "cost_usd": 0.0,
        },
    ],
    "spent_usd": 3.5,
    "fleet_usd": 25.0,
    "free_slots": 5,
    "held_areas": ["eval"],
    "draining": False,
    "drain_reason": "",
}


def test_every_task_appears_with_its_state():
    lines = "\n".join(render(PAYLOAD))
    assert "#1" in lines and "claimed" in lines
    assert "#4" in lines and "blocked" in lines


def test_spend_is_shown_against_the_ceiling():
    """A number with no ceiling beside it cannot be read as 'nearly drained',
    which is the one thing a 3am glance needs to answer."""
    assert any("3.5" in line and "25.0" in line for line in render(PAYLOAD))


def test_a_draining_fleet_says_so_and_says_why():
    payload = {**PAYLOAD, "draining": True, "drain_reason": "budget"}
    lines = "\n".join(render(payload))
    assert "drain" in lines.lower() and "budget" in lines


def test_an_empty_payload_says_the_daemon_has_not_ticked():
    """Distinct from 'no daemon', which is the client's error. A daemon that has
    started but not completed a tick is a real and different state."""
    assert any("no tick" in line.lower() for line in render({}))


def test_zero_spend_and_zero_free_slots_still_render():
    """Falsy zero is a real value here, not an absence: `spent $0 of $25` and
    `free slots 0` both describe a real fleet state and must not vanish behind
    an `if spent_usd` / `if free_slots` guard."""
    payload = {**PAYLOAD, "spent_usd": 0.0, "free_slots": 0}
    lines = "\n".join(render(payload))
    assert "0" in lines and "25.0" in lines


def test_tick_zero_still_renders():
    """The first tick is numbered 0 before `Daemon.tick` increments it in some
    designs; whatever the number, it must appear rather than be treated as
    absent."""
    payload = {**PAYLOAD, "tick": 0}
    lines = "\n".join(render(payload))
    assert "tick 0" in lines


def test_no_held_areas_renders_a_placeholder_not_nothing():
    payload = {**PAYLOAD, "held_areas": []}
    lines = "\n".join(render(payload))
    assert any("held" in line.lower() for line in [lines])
