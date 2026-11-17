from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest

from fleet import cli
from fleet import policy as policy_module
from fleet.chain_executor import ChainExecutor
from fleet.composite_executor import CompositeExecutor
from fleet.config import load_config
from fleet.gh import FakeGh
from fleet.herdr import FakeHerdr
from fleet.herdr_executor import HerdrExecutor
from fleet.model import (
    AgentState,
    Assign,
    CloseIssue,
    Comment,
    SetLabels,
    StartAgent,
    StopAgent,
)

FIXTURE_POLICY = Path(__file__).resolve().parents[1] / "fixtures/policy/fleet-fixture"
SPEC_BODY = "## Goal\n\nBuild the thing.\n\nSpec: docs/ai/specs/x.md\n"


class ScriptedCli:
    """A fake `run_cli` for chain. A subcommand with nothing scripted answers
    as an unknown invocation does (exit 2, no stdout), never as a success."""

    def __init__(self, **script: tuple[int, str, str]) -> None:
        self.script = script
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return self.script.get(argv[1], (2, "", f"chain: no scripted answer for {argv[1]}"))

    def calls_to(self, sub: str) -> list[list[str]]:
        return [argv for argv in self.calls if argv[1] == sub]


@pytest.fixture()
def fcfg(tmp_path):
    """The vendored fixture policy (build and bug route to chain), with every
    path this code writes pointed at tmp."""
    return dataclasses.replace(
        load_config(FIXTURE_POLICY),
        answers_dir=tmp_path / "answers",
        runlog_path=tmp_path / "state" / "run.jsonl",
        socket_path=tmp_path / "fleet.sock",
        worktree_root=tmp_path / "worktrees",
    )


def _parts(fcfg, cli_script: ScriptedCli | None = None):
    gh = FakeGh()
    herdr = FakeHerdr(worktree_root=fcfg.worktree_root)
    herdr_exec = HerdrExecutor(herdr, gh, fcfg)
    chain = ChainExecutor(
        fcfg,
        gh,
        "chain",
        cli_script or ScriptedCli(start=(0, "run-a\n", "")),
        fcfg.runlog_path.parent / "chain-runs.json",
    )
    policy = policy_module.load(fcfg)
    return gh, herdr, herdr_exec, chain, CompositeExecutor(herdr_exec, chain, policy)


def _issues(gh: FakeGh) -> tuple[int, int]:
    patch = gh.create_issue(
        "Fix LID", "b", ["fleet:claimed", "class:patch", "area:eval", "fleet:auto-ok"]
    )
    build = gh.create_issue(
        "Build it", SPEC_BODY, ["fleet:claimed", "class:build", "area:train", "fleet:auto-ok"]
    )
    return patch, build


def test_start_routes_by_kind(fcfg):
    gh, herdr, _, chain, composite = _parts(fcfg)
    patch, build = _issues(gh)
    composite.apply(
        [
            StartAgent(task=patch, role="implementer", base="integration"),
            StartAgent(task=build, role="chain", base="integration"),
        ]
    )
    # The claude role went to herdr, and only it did.
    assert [(task, role) for task, role, _ in herdr.started] == [(patch, "implementer")]
    # The chain role went to chain, and only it did.
    starts = chain.run_cli.calls_to("start")
    assert len(starts) == 1
    assert f"{fcfg.repo_slug}#{build}" in starts[0]
    assert chain.tasks() == {build}


def test_start_of_a_chain_role_with_no_chain_executor_raises(fcfg):
    """Wiring builds a ChainExecutor whenever a route names a chain role, so
    this is a defect identical for every such task. Handing it to herdr would
    start a claude pane for a chain role."""
    gh = FakeGh()
    herdr = FakeHerdr(worktree_root=fcfg.worktree_root)
    composite = CompositeExecutor(HerdrExecutor(herdr, gh, fcfg), None, policy_module.load(fcfg))
    _, build = _issues(gh)
    with pytest.raises(ValueError, match="chain"):
        composite.apply([StartAgent(task=build, role="chain", base="integration")])
    assert herdr.started == []


def test_stop_routes_by_owner(fcfg):
    cli_script = ScriptedCli(start=(0, "run-a\n", ""), abort=(0, "", ""))
    gh, herdr, _, chain, composite = _parts(fcfg, cli_script)
    patch, build = _issues(gh)
    composite.apply(
        [
            StartAgent(task=patch, role="implementer", base="integration"),
            StartAgent(task=build, role="chain", base="integration"),
        ]
    )
    composite.apply([StopAgent(task=build), StopAgent(task=patch)])
    # chain aborted its own run and nothing else.
    assert cli_script.calls_to("abort") == [["chain", "abort", "run-a"]]
    assert chain.tasks() == set()
    # herdr stopped its own agent and was never asked to stop the chain task.
    assert herdr.stopped == [patch]
    assert build not in herdr.removed


def test_stop_of_a_task_chain_does_not_own_goes_to_herdr(fcfg):
    """An orphaned herdr agent on a build row (a worktree from before the route
    changed) is herdr's to stop: chain has no run for it."""
    cli_script = ScriptedCli(abort=(0, "", ""))
    gh, herdr, _, _, composite = _parts(fcfg, cli_script)
    herdr.add_agent(task=7, status="working", change_seq=1)
    composite.apply([StopAgent(task=7)])
    assert herdr.stopped == [7]
    assert cli_script.calls_to("abort") == []


def test_ledger_effects_applied_once(fcfg):
    gh, _, _, _, composite = _parts(fcfg)
    gh.known_logins = {"someone"}
    patch, _ = _issues(gh)
    composite.apply(
        [
            SetLabels(task=patch, add=("fleet:review",), remove=("fleet:claimed",)),
            Comment(task=patch, body="one comment"),
            Assign(task=patch, owner="someone"),
            CloseIssue(task=patch),
        ]
    )
    issue = gh.get_issue(patch)
    assert issue["comments"].count("one comment") == 1
    assert len(issue["comments"]) == 1
    assert issue["labels"].count("fleet:review") == 1
    assert "fleet:claimed" not in issue["labels"]
    assert issue["state"] == "CLOSED"


def test_effects_keep_their_order_across_executors(fcfg):
    """A task's claim (SetLabels) precedes its start in the batch, and chain's
    start reads the class off the labels it finds."""
    gh, _, _, chain, composite = _parts(fcfg)
    build = gh.create_issue("Build it", SPEC_BODY, ["fleet:ready", "fleet:auto-ok", "area:train"])
    composite.apply(
        [
            SetLabels(task=build, add=("fleet:claimed", "class:build"), remove=("fleet:ready",)),
            StartAgent(task=build, role="chain", base="integration"),
        ]
    )
    assert len(chain.run_cli.calls_to("start")) == 1
    assert list(chain.failures) == []


def test_agents_is_the_union(fcfg):
    status = json.dumps({"schema": "chain.status.v1", "runs": []})
    gh, herdr, _, chain, composite = _parts(fcfg, ScriptedCli(status=(0, status, "")))
    herdr.add_agent(task=3, status="working", change_seq=4)
    chain._failed_start.add(9)
    assert composite.agents() == [
        AgentState(task=3, status="working", change_seq=4),
        AgentState(task=9, status="dirty", change_seq=0),
    ]


def test_a_task_reported_by_both_is_chains_and_recorded_once(fcfg):
    """A task in both lists is a defect somewhere (a herdr worktree left on a
    row chain now runs). decide keys agents by task, so passing both through
    would let list order pick one silently. chain's wins, since StopAgent goes
    to chain for a task it owns: the Retire that stops it reaches the agent
    that was reported. The herdr agent shows next tick, once chain's is gone."""
    status = json.dumps({"schema": "chain.status.v1", "runs": []})
    gh, herdr, _, chain, composite = _parts(fcfg, ScriptedCli(status=(0, status, "")))
    herdr.add_agent(task=9, status="working", change_seq=4, cost_usd=1.0)
    chain._failed_start.add(9)
    expected = [AgentState(task=9, status="dirty", change_seq=0)]
    assert composite.agents() == expected
    assert composite.agents() == expected
    duplicates = [line for line in composite.failures if "task 9" in line and "both" in line]
    assert len(duplicates) == 1
    # Resolved, then back: recorded again.
    chain._failed_start.discard(9)
    assert composite.agents() == [AgentState(task=9, status="working", change_seq=4, cost_usd=1.0)]
    chain._failed_start.add(9)
    composite.agents()
    assert len([line for line in composite.failures if "both" in line]) == 2


def test_unpriced_is_the_union(fcfg):
    _, herdr, _, _, composite = _parts(fcfg)
    herdr.unpriced_models = {"gpt-9"}
    assert composite.unpriced() == {"gpt-9"}


def test_failures_drain_once(fcfg):
    """`Daemon._drain_failures` empties `executor.failures` with popleft until
    it is falsy. A property that merged two deques on each read would never
    empty, so the composite exposes one deque that both executors append to."""
    gh, _, herdr_exec, chain, composite = _parts(fcfg, ScriptedCli())
    assert composite.failures is herdr_exec.failures is chain.failures
    composite.apply([StopAgent(task=4)])  # herdr: agent fleet-4 not found
    composite.agents()  # chain: status exits 2
    drained = []
    for _ in range(10):  # bounded: a merging property would loop here forever
        if not composite.failures:
            break
        drained.append(composite.failures.popleft())
    assert len(drained) == 2
    assert any("could not stop agent" in line for line in drained)
    assert any("chain status exited 2" in line for line in drained)
    assert not composite.failures


def test_owner_is_herdrs(fcfg):
    gh = FakeGh()
    herdr_exec = HerdrExecutor(FakeHerdr(), gh, fcfg, owner="fleet-bot")
    composite = CompositeExecutor(herdr_exec, None, policy_module.load(fcfg))
    assert composite.owner == "fleet-bot"


# --- the cli wiring --------------------------------------------------------------


def _unrouted_chain_policy(tmp_path) -> Path:
    """The fixture policy with the build and bug routes removed. The chain row
    of the Role | Agent | Model table stays: a declared but unrouted role is
    not a reason to run chain."""
    root = tmp_path / "target"
    shutil.copytree(FIXTURE_POLICY, root)
    classes = root / "docs/agent/task-classes.md"
    lines = classes.read_text().splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(("| `build` |", "| `bug` |"))]
    assert len(kept) == len(lines) - 2
    classes.write_text("".join(kept))
    return root


def test_no_chain_roles_no_chain_executor(tmp_path):
    cfg = dataclasses.replace(
        load_config(_unrouted_chain_policy(tmp_path)), runlog_path=tmp_path / "run.jsonl"
    )
    policy = policy_module.load(cfg)
    assert "chain" in {role for role, _, _ in policy.agents}
    gh = FakeGh()
    herdr_exec = HerdrExecutor(FakeHerdr(), gh, cfg)
    composite = cli.build_executor(cfg, policy, gh, herdr_exec, run_cli=ScriptedCli())
    assert composite.chain is None
    assert composite.herdr is herdr_exec


def test_a_chain_route_builds_a_chain_executor(fcfg):
    policy = policy_module.load(fcfg)
    gh = FakeGh()
    herdr_exec = HerdrExecutor(FakeHerdr(), gh, fcfg)
    run_cli = ScriptedCli()
    composite = cli.build_executor(fcfg, policy, gh, herdr_exec, run_cli=run_cli)
    chain = composite.chain
    assert chain is not None
    assert chain.command == fcfg.agents["chain"].command
    assert chain.run_cli is run_cli
    assert chain.gh is gh
    assert chain.state_path == fcfg.runlog_path.parent / "chain-runs.json"
    assert chain.failures is herdr_exec.failures
