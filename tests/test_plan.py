from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import FIXTURE_POLICY
from fleet.cli import NO_AGENTS_CAVEAT, ReadOnlyGh, do_plan, main
from fleet.config import load_config
from fleet.gh import FakeGh, GhClient, RealGh
from fleet.simulate import run

T0 = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)

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

    `do_plan` now reads through `reader.read_ledger`, whose query is `all`, so
    the two row loaders see the same rows by construction rather than by
    coincidence. The obligation stands anyway: this is the only test that
    compares the operator's preview against the simulator's tick, and the next
    divergence will not be the query.
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


# -- the preview must be the daemon's own reading ---------------------------


def _handoff(**fields):
    from fleet import packet as packet_module

    return packet_module.render({"schema": "fleet.handoff.v1", **fields})


def test_the_preview_sees_a_blocked_task_whose_dependency_has_closed():
    """`do_plan` read `gh.list_issues("open")` and passed no packets, so it gave
    a systematically different answer from the daemon -- which reads through
    `reader.read_ledger`. Two independent holes, both on this row:
    `Task.blocked_on` comes from the packet and nothing else, so with no packets
    it was always None; and the closed dependency that satisfies it is invisible
    to an open-only query. The operator's preview -- and Task 21's pre-flight
    check -- therefore reported no UNBLOCK on a fleet that would emit one."""
    gh = FakeGh()
    blocked = gh.create_issue("t", "b", ["fleet:blocked", "fleet:auto-ok", "area:models"])
    gh.comment(blocked, _handoff(task=blocked, to="blocked", blocked_on=2))
    dependency = gh.create_issue("dep", "b", ["fleet:done", "area:data"])
    gh.close_issue(dependency)
    assert "Unblock(1)" in do_plan(load_config(FIXTURE_POLICY), ReadOnlyGh(gh), T0)


def test_the_preview_reads_an_integrator_handback_as_a_rebase():
    """The other half of the same divergence. `_is_rebase_handback` reads
    `task.packet`, and a preview that fetches no packets sees a CLAIMED row with
    no agent and calls it ledger drift -- so it predicts `Release(no_agent)`
    where the daemon emits `Rebase`. An operator running this as a pre-flight
    check is told the fleet is about to burn an attempt it is not."""
    gh = FakeGh()
    task = gh.create_issue(
        "t", "b", ["fleet:claimed", "fleet:attempt-1", "fleet:auto-ok", "class:integration"]
    )
    gh.comment(
        task,
        _handoff(
            task=task, to="claimed", attempt=1, **{"from": {"role": "integrator", "agent": "f-1"}}
        ),
    )
    lines = do_plan(load_config(FIXTURE_POLICY), ReadOnlyGh(gh), T0)
    assert "Rebase(1, 1)" in lines
    assert not any(line.startswith("Release(") for line in lines)


def test_plan_has_no_no_dry_run_flag_at_all():
    """`fleet plan` reads and renders and makes no write, so `--no-dry-run`
    selected an unwrapped writing client for a body that never used it. The
    flag did nothing, and the test that claimed to pin it monkeypatched
    `do_plan` away, so it could not have noticed. A flag that does nothing is
    worse than no flag: an operator who passes it believes something happened."""
    result = CliRunner().invoke(main, ["plan", "--no-dry-run"])
    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_plan_says_it_is_read_only_in_its_help():
    """The replacement for the flag. Without it the only way to learn that
    `plan` never writes is to read `ReadOnlyGh`."""
    result = CliRunner().invoke(main, ["plan", "--help"])
    assert result.exit_code == 0
    assert "writes nothing" in result.output


def test_the_plan_previews_an_adoption_with_the_owners_login(policy_cfg, monkeypatch):
    """`fleet plan` claims to give the answer the next tick would. The daemon
    reads the ledger with gh's login, so the preview must too: without it no row
    is the owner's and an Adopt the daemon will make never shows."""
    import dataclasses

    cfg = dataclasses.replace(policy_cfg, chain_default_area="eval")
    gh = FakeGh()
    gh.ensure_label("agent-reported")
    number = gh.create_issue("lid rounds early", "b", ["agent-reported"])

    assert do_plan(cfg, ReadOnlyGh(gh), datetime.now(UTC), login="FibonAdithya") == [
        f"Adopt({number})"
    ]
    assert do_plan(cfg, ReadOnlyGh(gh), datetime.now(UTC)) == ["no action"]

    monkeypatch.setattr("fleet.cli.load_config", lambda: cfg)
    monkeypatch.setattr("fleet.cli.RealGh", lambda slug: gh)
    monkeypatch.setattr("fleet.cli._gh_login", lambda: "FibonAdithya")
    result = CliRunner().invoke(main, ["plan"])
    assert result.exit_code == 0, result.output
    assert f"Adopt({number})" in result.output
