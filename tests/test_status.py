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


FAILING = {
    **PAYLOAD,
    "failures": [
        "task 1: could not remove worktree (code dirty_worktree_requires_force): boom",
        "tick 7 failed: gh issue list failed: HTTP 503",
    ],
}


def test_swallowed_failures_are_rendered():
    """The design puts every swallowed failure in `FleetSnapshot.failures` and
    makes `fleet status` the one place an operator reads them. Nothing rendered
    them, so a herdr that failed every worktree removal all night looked exactly
    like a quiet night -- the failure the bounded deque exists to prevent,
    reinstated one layer further out."""
    lines = "\n".join(render(FAILING))
    assert "could not remove worktree" in lines
    assert "HTTP 503" in lines


def test_the_failure_count_is_visible_before_the_table():
    """Sixty-four failures rendered only at the bottom of a fifty-row table are
    off the screen of the `watch fleet status` they are for. The count goes in
    the header block, where the spend and the free slots are, so a glance
    catches it and the detail is there to scroll to."""
    lines = render(FAILING)
    header = lines[: lines.index(_header_row(lines))]
    assert any("2" in line and "failure" in line.lower() for line in header)


def _header_row(lines):
    return next(line for line in lines if line.strip().startswith("task"))


def test_a_fleet_with_no_failures_says_nothing_about_them():
    """Printed unconditionally it is decoration, and decoration is skipped --
    the same rule the drain line follows."""
    lines = "\n".join(render({**PAYLOAD, "failures": []}))
    assert "failure" not in lines.lower()


def test_a_payload_with_no_failures_key_at_all_still_renders():
    """A snapshot from a daemon older than the field, served over a socket a
    newer client connected to. Rendering must not KeyError -- the client would
    exit non-zero and read as 'no fleet daemon', which is a different fact."""
    payload = {k: v for k, v in PAYLOAD.items() if k != "failures"}
    assert render(payload)
