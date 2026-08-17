from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import FIXTURE_POLICY
from fleet.cli import NO_AGENTS_CAVEAT, ReadOnlyGh, do_plan
from fleet.config import load_config
from fleet.gh import FakeGh, GhClient, RealGh
from fleet.simulate import run

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)

ROOT = Path(__file__).resolve().parents[1]
CAPTURED = "fixtures/ledger/issues.json"

READY = ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"]
CLAIMED = ["fleet:claimed", "fleet:auto-ok", "fleet:attempt-1", "area:eval", "class:patch"]


def public_methods(cls) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_")}


@pytest.fixture()
def policy_cfg():
    """conftest's `cfg` builds a tmp repo with schemas and no policy tables;
    policy.load needs ownership.md and task-classes.md, so this is the vendored
    fixture layer itself."""
    return load_config(FIXTURE_POLICY)


@pytest.fixture()
def gh():
    client = FakeGh()
    client.create_issue("t", "b", READY)
    return client


def test_every_public_method_outside_the_read_allowlist_is_refused(gh):
    """Enumerated from the clients, not listed by hand. `close_issue` is on
    FakeGh, on no Protocol, and mutates -- a wrapper that names its writes lets
    the next one of those through, and the previous test only read the Protocol
    so it would not have noticed."""
    surface = public_methods(GhClient) | public_methods(RealGh) | public_methods(FakeGh)
    refused = sorted(surface - ReadOnlyGh.READS)
    assert "close_issue" in refused  # the enumeration reaches past the Protocol
    for name in refused:
        with pytest.raises(PermissionError, match="dry-run"):
            getattr(ReadOnlyGh(gh), name)()


def test_reads_still_work(gh):
    guarded = ReadOnlyGh(gh)
    assert guarded.list_issues("all")[0]["number"] == 1
    assert guarded.get_issue(1)["title"] == "t"


def test_the_ledger_is_unchanged_after_a_refused_write(gh):
    """ "Refused" has to mean nothing happened, not that an exception followed
    the write."""
    guarded = ReadOnlyGh(gh)
    with pytest.raises(PermissionError):
        guarded.comment(1, "hi")
    assert gh.get_issue(1)["comments"] == []


def test_the_plan_renders_intents_in_the_scenarios_own_notation(policy_cfg, gh):
    """`f"{type(intent).__name__}: {intent}"` printed `Dispatch: Dispatch(task=1,
    role='implementer', ...)` -- the class name twice, in a notation nothing else
    in the project uses. render_intent is the one the scenarios are written in."""
    assert do_plan(policy_cfg, ReadOnlyGh(gh), T0) == ["Dispatch(1, implementer, 1)"]


def test_a_claimed_task_gets_the_caveat_about_having_no_agents(policy_cfg):
    """M2 has no agent source, so every CLAIMED task reaps as ledger drift.
    Printing that Release without saying so tells an operator whose fleet is
    running to release every task it is working on."""
    client = FakeGh()
    client.create_issue("t", "b", CLAIMED)
    lines = do_plan(policy_cfg, ReadOnlyGh(client), T0)
    assert "no live agents" in lines[0]
    assert "Release(1, no_agent, 2)" in lines


def test_the_caveat_is_absent_when_nothing_is_claimed(policy_cfg, gh):
    """Printed unconditionally it is decoration, and decoration is skipped."""
    assert all("no live agents" not in line for line in do_plan(policy_cfg, ReadOnlyGh(gh), T0))


def test_an_idle_ledger_says_so(policy_cfg):
    assert do_plan(policy_cfg, ReadOnlyGh(FakeGh()), T0) == ["no action"]


def _captured_into(gh: FakeGh) -> None:
    """The capture, replayed through the client's own create/close surface.

    Ascending by number so FakeGh's sequential allocator reproduces the captured
    numbers -- asserted, not assumed, because an expectation naming issue 9 is
    worthless if the fake called it something else.
    """
    for row in sorted(json.loads((ROOT / CAPTURED).read_text()), key=lambda r: r["number"]):
        number = gh.create_issue(row["title"], row["body"], [x["name"] for x in row["labels"]])
        assert number == row["number"]
        if row["state"].upper() == "CLOSED":
            gh.close_issue(number)


def test_the_simulators_row_loader_and_list_issues_open_agree(policy_cfg):
    """Design §10's parity row, as a standing obligation.

    `do_plan` reads `gh.list_issues("open")`; the simulator's `_rows` reads a
    captured file that includes CLOSED rows. Nothing checked that decide() gave
    both the same answer, and it did not: closed issue 12 still carries
    fleet:review and area:docs, so through the simulator it silently held a slot
    and locked area:docs while `fleet plan` never saw it at all. That is the
    fake/real divergence class ledger.py exists to concentrate, and it survived
    the whole suite because no test compared the two paths.
    """
    gh = FakeGh()
    _captured_into(gh)
    lines = [line for line in do_plan(policy_cfg, ReadOnlyGh(gh), T0) if line != NO_AGENTS_CAVEAT]
    assert lines and lines != ["no action"]  # a parity test on two empty lists proves nothing

    # `run` raises AssertionError unless the simulator's own tick 1 is exactly
    # this multiset, so the comparison is the assertion.
    run(
        {
            "name": "parity",
            "proves": "the two row loaders agree about the captured ledger",
            "seed": {"ledger": CAPTURED, "policy": str(FIXTURE_POLICY)},
            "ticks": [{"outcomes": {}, "expect": lines}],
        },
        ROOT,
    )
