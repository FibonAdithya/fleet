from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import get_args

import pytest

from fleet.model import (
    AgentState,
    Dispatch,
    Effect,
    FleetState,
    Intent,
    Observation,
    Policy,
    Release,
    Task,
)


def _policy(**kw) -> Policy:
    fields = {
        "areas": (("src/eval/**", "eval"),),
        "routes": (("patch", ("implementer", "reviewer", "integrator")),),
        "slots": 6,
        "stall_minutes": 10,
        "max_attempts": 2,
        "max_fix_passes": 1,
        "max_rebases": 1,
        "per_task_usd": 2.0,
        "fleet_usd": 25.0,
        "integration_branch": "integration",
    }
    return Policy(**{**fields, **kw})


def test_a_task_carrying_a_packet_is_still_hashable():
    """Catches dropping `hash=False` from `packet`/`verdict`: a frozen dataclass
    hashes every compared field, so a populated packet makes `hash(task)` raise
    TypeError. Hashing a packet-less task -- which is what this test used to do
    -- proves nothing, because the dict is the whole problem."""
    task = Task(
        number=1,
        title="t",
        state="review",
        area="eval",
        klass="patch",
        auto_ok=True,
        packet={"to": "review", "blocked_on": None},
        verdict={"verdict": "approved"},
    )
    assert isinstance(hash(task), int)
    assert len({task, task}) == 1


def test_a_task_is_frozen():
    """Catches removing `frozen=True`: a caller that edits what it was given
    corrupts a shared snapshot, which is the M1 defect where FakeGh handed out
    its live `labels` list."""
    task = Task(number=1, title="t", state="ready", area="eval", klass="patch", auto_ok=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.state = "claimed"


def test_task_defaults_are_the_absent_case_not_the_zero_case():
    """Catches defaulting `packet` to `{}` or a counter to 1. `decide()` reads
    `attempts` as "how many have been spent", so a default of 1 spends one."""
    task = Task(number=1, title="t", state="triage", area=None, klass=None, auto_ok=False)
    assert task.attempts == 0
    assert task.fix_passes == 0
    assert task.packet is None
    assert task.verdict is None
    assert task.blocked_on is None


def test_intents_compare_by_value_including_the_attempt():
    """Catches `eq=False`, and catches an intent that drops `attempt` from
    equality: `decide()`'s tests assert on intent equality, so two dispatches
    differing only in attempt number must not compare equal."""
    assert Dispatch(task=1, role="implementer", base="integration", attempt=1) == Dispatch(
        task=1, role="implementer", base="integration", attempt=1
    )
    assert Dispatch(task=1, role="implementer", base="integration", attempt=1) != Dispatch(
        task=1, role="implementer", base="integration", attempt=2
    )
    assert Release(task=1, reason="stalled", attempt=2) != Release(
        task=1, reason="stalled", attempt=1
    )


def test_dispatch_field_order_is_the_render_order():
    """Catches reordering `Dispatch`'s fields. `simulate.render_intent` prints
    every field but `base` in declaration order, so a scenario's
    `Dispatch(5, implementer, 1)` silently becomes a different string if the
    attempt moves ahead of the role."""
    names = [f.name for f in dataclasses.fields(Dispatch)]
    assert names == ["task", "role", "base", "attempt"]


def test_policy_route_for_returns_an_empty_tuple_for_an_unknown_class():
    """Catches `route_for` returning None: `_dispatch` reads `route[0]` after a
    falsiness check, and None passes the check but raises on the subscript."""
    policy = _policy()
    assert policy.route_for("patch") == ("implementer", "reviewer", "integrator")
    assert policy.route_for("nonexistent") == ()
    assert policy.route_for(None) == ()


def test_policy_carries_the_integration_branch():
    """Catches hardcoding `base="integration"` in `_dispatch`. A target repo
    whose integration branch is named anything else would otherwise have every
    agent started off the wrong base, and no test would notice."""
    assert _policy(integration_branch="trunk").integration_branch == "trunk"


def test_agent_state_carries_what_a_snapshot_cannot():
    """Catches dropping `task` or `cost_usd`: herdr's snapshot has no timestamp
    and no issue number -- only agent_status, state_change_seq and a worktree
    cwd. The shell supplies both missing facts; this type is where they arrive."""
    state = AgentState(task=5, status="working", change_seq=551, cost_usd=0.4)
    assert (state.task, state.cost_usd) == (5, 0.4)


def test_fleet_state_looks_an_observation_up_by_task():
    """Catches an `observation` that returns the whole `seen` tuple or raises on
    an unknown task: the first tick a task is seen has no prior observation, and
    stall detection asks for it anyway."""
    obs = Observation(change_seq=551, since=datetime(2026, 8, 17, tzinfo=timezone.utc))
    state = FleetState(seen=((5, obs),))
    assert state.observation(5) is obs
    assert state.observation(6) is None


def test_no_intent_or_effect_exists_that_nothing_emits():
    """Catches re-adding `CreateIntegrationTask` or `CreateIssue`. A union member
    no phase of `decide()` emits is either a dead branch in `expand()` or an
    unhandled member that falls through to a silent no-op. Epic fan-in is M3;
    when M3 wants these back it re-adds them together with their emitter."""
    assert {t.__name__ for t in get_args(Intent)} == {
        "Dispatch",
        "Continue",
        "Release",
        "Rebase",
        "Escalate",
        "Unblock",
        "Advance",
        "Retire",
        "Drain",
    }
    assert {t.__name__ for t in get_args(Effect)} == {
        "SetLabels",
        "Assign",
        "StartAgent",
        "StopAgent",
        "CloseIssue",
    }
