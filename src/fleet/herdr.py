"""The herdr seam. RealHerdr shells out; FakeHerdr is in memory.

Same shape as `gh.py`, for the same reason: four of M0-M1's defects were cases
where the fake was subtly more permissive than the real client, and every one of
them passed the whole offline suite (docs/m2-inputs.md §6).

A task's agent is identified by its worktree path, not by anything herdr stores:
the snapshot carries no issue number. The executor chooses the path, so the
daemon can parse it -- which is what makes a restart cheap, because the answer to
"who is working on #1" is a directory on disk plus a label on GitHub, and both
survive kill -9.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from fleet.cost import price_transcript, transcript_path
from fleet.model import AgentState

# herdr's five statuses to AgentState's three.
#
# `blocked` is dirty rather than working: it means the agent is waiting for input
# nothing will give it -- a permission prompt, which parent §8 names as the most
# common cause of a stall. Waiting stall_minutes to notice what herdr has already
# told us is ten wasted minutes per occurrence.
#
# `idle` is done: the dispatcher submits exactly one prompt, so an agent back at
# idle has finished that turn and written whatever packet it was going to write.
#
# `unknown` is working: herdr saying it cannot tell is not evidence of failure.
# Treating it as one would release healthy agents; a real hang is still caught by
# the stall timer, which is what that timer is for.
STATUS = {
    "working": "working",
    "unknown": "working",
    "idle": "done",
    "done": "done",
    "blocked": "dirty",
}

_LEADING_NUMBER = re.compile(r"^(\d+)(?:-|$)")


class HerdrError(RuntimeError):
    """Anything the herdr client could not do."""


def task_of(checkout_path: str, worktree_root: Path) -> int | None:
    """`<worktree_root>/<n>-<slug>` -> n, else None.

    None for every workspace outside the fleet's own worktree root. A human's
    herdr session on the same box must not be read as a fleet agent: it would be
    counted against `slots` and REAP would try to stop it.
    """
    try:
        relative = Path(checkout_path).resolve().relative_to(Path(worktree_root).resolve())
    except ValueError:
        return None
    if not relative.parts:
        return None
    match = _LEADING_NUMBER.match(relative.parts[0])
    return int(match.group(1)) if match else None


def parse_snapshot(
    raw: dict,
    worktree_root: Path,
    projects_dir: Path | None = None,
    prices: dict[str, dict[str, float]] | None = None,
) -> list[AgentState]:
    """herdr's snapshot JSON -> AgentState, cost joined.

    `cwd` rather than the workspace's `worktree.checkout_path`: the agent record
    carries it directly, and going via `workspace_id` would silently drop an
    agent whose workspace is missing from the same snapshot.

    Pricing an unpriced model is not swallowed here: `price_transcript` raises
    `UnpricedModel` on purpose (a zero price would switch the budget ceiling off
    silently), and it is left to propagate out of this call rather than caught
    and defaulted to zero -- the caller (the daemon tick, Task 15) is specified
    to catch it and drain.
    """
    agents = (raw.get("result") or {}).get("snapshot", {}).get("agents", [])
    out: list[AgentState] = []
    for agent in agents:
        task = task_of(agent.get("cwd") or "", worktree_root)
        if task is None:
            continue
        status = STATUS.get(agent.get("agent_status"), "working")
        session = (agent.get("agent_session") or {}).get("value")
        cost = 0.0
        if session and projects_dir is not None and prices:
            path = transcript_path(session, projects_dir)
            if path is not None:
                cost = price_transcript(path, prices)
        out.append(
            AgentState(
                task=task,
                status=status,
                change_seq=int(agent.get("state_change_seq", 0)),
                cost_usd=cost,
            )
        )
    return sorted(out, key=lambda a: a.task)


class Herdr(Protocol):
    def agents(self) -> list[AgentState]: ...
    def create_worktree(self, task: int, slug: str, base: str) -> Path: ...
    def remove_worktree(self, task: int) -> None: ...
    def start_agent(self, task: int, role: str, prompt: str) -> None: ...
    def stop_agent(self, task: int) -> None: ...


class FakeHerdr:
    """In-memory, with the same surface. The only client tests use."""

    def __init__(self, worktree_root: Path | None = None) -> None:
        self.root = Path(worktree_root or "/fake/worktrees")
        self._agents: dict[int, AgentState] = {}
        self.worktrees: dict[int, Path] = {}
        self.started: list[tuple[int, str, str]] = []
        self.stopped: list[int] = []
        self.removed: list[int] = []

    def add_agent(self, task: int, status: str, change_seq: int, cost_usd: float = 0.0) -> None:
        if status not in ("working", "done", "dirty"):
            # A typo would otherwise script a working agent and the test would
            # pass while proving nothing -- the same guard FakeExecutor.script has.
            raise ValueError(f"unknown status {status!r}")
        self._agents[task] = AgentState(
            task=task, status=status, change_seq=change_seq, cost_usd=cost_usd
        )

    def agents(self) -> list[AgentState]:
        return sorted(self._agents.values(), key=lambda a: a.task)

    def create_worktree(self, task: int, slug: str, base: str) -> Path:
        path = self.root / f"{task}-{slug}"
        self.worktrees[task] = path
        return path

    def remove_worktree(self, task: int) -> None:
        self.worktrees.pop(task, None)
        self.removed.append(task)

    def start_agent(self, task: int, role: str, prompt: str) -> None:
        self.started.append((task, role, prompt))
        self.add_agent(task, "working", change_seq=len(self.started))

    def stop_agent(self, task: int) -> None:
        self._agents.pop(task, None)
        self.stopped.append(task)
