"""ADOPT: an issue the owner's own agent reported, with no fleet state yet, is
handed to the chain route as a `bug` without waiting for a human to triage it.

Every condition here is one an issue that must stay with a human fails: written
by someone else, too old, already given a state, or a target with no chain route
or no default area to file it under.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from fleet.decide import REAPING, decide
from fleet.expand import expand
from fleet.model import Adopt, FleetState, Policy, SetLabels, Task

T0 = datetime(2026, 11, 17, 2, 0, tzinfo=UTC)

CHAIN_POLICY = Policy(
    areas=(("src/eval/**", "eval"), ("src/data/**", "data")),
    routes=(
        ("patch", ("implementer", "reviewer", "integrator")),
        ("bug", ("chain", "integrator")),
    ),
    agents=(
        ("implementer", "claude", "claude-opus-5"),
        ("reviewer", "claude", "claude-opus-5"),
        ("integrator", "claude", "claude-opus-5"),
        ("chain", "chain", "chain"),
    ),
    integration_branch="integration",
    slots=6,
    stall_minutes=10,
    max_attempts=2,
    max_fix_passes=1,
    max_rebases=1,
    per_task_usd=2.0,
    fleet_usd=25.0,
    chain_default_area="eval",
    chain_max_age_days=7,
)


def reported(number=9, **kw):
    """What read_one makes of an issue the box filed: no fleet state label (so
    it reads as triage), the `agent-reported` label, written by the owner."""
    fields = dict(
        number=number,
        title="lid rounds early",
        state="triage",
        area=None,
        klass=None,
        auto_ok=False,
        agent_reported=True,
        by_owner=True,
        has_state_label=False,
        created_at=T0 - timedelta(days=1),
    )
    fields.update(kw)
    return Task(**fields)


def adopted(ledger, policy=CHAIN_POLICY, now=T0):
    intents, _ = decide(FleetState(), ledger, policy, [], now)
    return [i for i in intents if isinstance(i, Adopt)]


def test_adopt_agent_reported():
    """The positive case. Catches the phase being absent, or not wired into
    decide()."""
    assert adopted([reported()]) == [Adopt(task=9)]


def test_no_adopt_other_author():
    """Anyone can open an issue on a public repo and label it agent-reported.
    Only the owner's login reads as the owner's agent; catches dropping the
    `by_owner` condition."""
    assert adopted([reported(by_owner=False)]) == []


def test_no_adopt_older_than_max_age():
    """An old agent-reported issue sat untouched for a reason. "No older than
    `chain_max_age_days`" is inclusive: exactly the limit is adopted and one
    second past it is refused, so flipping `>=` to `>` fails the first case and
    ignoring the age fails the second. An issue with no createdAt (a captured
    row from before the field was fetched) is never adopted."""
    limit = timedelta(days=CHAIN_POLICY.chain_max_age_days)
    assert adopted([reported(created_at=T0 - limit)]) == [Adopt(task=9)]
    assert adopted([reported(created_at=T0 - limit - timedelta(seconds=1))]) == []
    assert adopted([reported(created_at=T0 - limit + timedelta(seconds=1))]) == [Adopt(task=9)]
    assert adopted([reported(created_at=None)]) == []


def test_no_default_area_no_adoption():
    """Review focus 5: a target whose fleet.toml routes `bug` to chain but has
    no `[chain] default_area` leaves agent-reported issues alone. Adopting with
    no area would file a task area exclusion cannot hold."""
    policy = dataclasses.replace(CHAIN_POLICY, chain_default_area=None)
    assert adopted([reported()], policy) == []


def test_no_adopt_without_a_chain_bug_route():
    """With `bug` routed to herdr roles, or not routed at all, the chain route
    is not enabled and nothing is adopted."""
    herdr_bug = dataclasses.replace(
        CHAIN_POLICY,
        routes=(
            ("patch", ("implementer", "reviewer", "integrator")),
            ("bug", ("implementer", "reviewer", "integrator")),
        ),
    )
    no_bug = dataclasses.replace(
        CHAIN_POLICY, routes=(("patch", ("implementer", "reviewer", "integrator")),)
    )
    assert adopted([reported()], herdr_bug) == []
    assert adopted([reported()], no_bug) == []


def test_no_adopt_with_state_label():
    """R11: a row with no state label and a row deliberately labelled
    `fleet:triage` both read as state triage. The labelled one is a human's
    decision to triage it, so only `has_state_label` tells them apart. Any
    other state is refused too."""
    assert adopted([reported(has_state_label=True)]) == []
    assert adopted([reported(state="human", has_state_label=True)]) == []


def test_no_adopt_when_not_agent_reported_or_closed():
    assert adopted([reported(agent_reported=False)]) == []
    assert adopted([reported(is_open=False)]) == []


def test_no_adopt_when_its_labels_would_conflict():
    """Adopt adds `class:bug` and the default area. An issue already carrying a
    different class or area would end with two of either, which read_one
    refuses for the whole ledger."""
    assert adopted([reported(klass="patch")]) == []
    assert adopted([reported(area="data")]) == []
    assert adopted([reported(klass="bug", area="eval")]) == [Adopt(task=9)]


def test_adopt_stops_no_agent():
    """Adopt starts nothing and stops nothing, so it is not a reaping intent:
    in REAPING it would finalise the cost of an agent that is not there."""
    assert Adopt not in REAPING


def test_adopt_expands_to_ready_bug_labels():
    """One SetLabels with the four labels that make the row dispatchable to the
    chain route next tick. `agent-reported` is outside the label vocabulary and
    is never touched; no state label but ready may survive."""
    effects = expand(Adopt(task=9), CHAIN_POLICY)
    assert len(effects) == 1
    (move,) = effects
    assert isinstance(move, SetLabels)
    assert move.task == 9
    assert move.add == ("fleet:ready", "class:bug", "fleet:auto-ok", "area:eval")
    assert "fleet:ready" not in move.remove
    assert "agent-reported" not in move.remove
