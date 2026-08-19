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

import json
import re
import subprocess
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

    Resolution order for the path is `cwd` first, `foreground_cwd` as a
    fallback when `cwd` yields no task. `cwd` is preferred because it is the
    pane's own directory and cannot be moved by an agent wandering with `cd`;
    `foreground_cwd` alone or first would let a wandering agent be
    misattributed to whatever fleet worktree it happened to `cd` into. The
    fallback exists because this box's own captured snapshot
    (fixtures/herdr/snapshot-fleet.json) shows a real case of an agent
    working inside a nested worktree while `cwd` stayed pinned at the main
    checkout and only `foreground_cwd` tracked the worktree -- if fleet's own
    agents behave the same way, reading `cwd` alone would silently drop a
    genuinely running fleet agent with no exception, which is worse than a
    crash (a stale slot, and a possible duplicate dispatch into an occupied
    worktree). That evidence is from a *different* tool's worktree handling,
    not fleet's.

    Task 10 left the fleet-specific case as an open gate item; Task 11 closed
    it by measurement. A real agent started the way `RealHerdr.start_agent`
    starts one -- `herdr worktree create` for the checkout, then `herdr agent
    start ... --kind claude --pane <id>` -- reports the *worktree* in `cwd`,
    with `foreground_cwd` identical. The capture is
    fixtures/herdr/agent-fleet-worktree.json and
    test_a_real_fleet_agent_reports_its_worktree_in_cwd pins it. So `cwd`
    first is the right order for fleet's own agents, and the `foreground_cwd`
    fallback is now belt-and-braces for the nested-worktree shape rather than
    the primary path.

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
            task = task_of(agent.get("foreground_cwd") or "", worktree_root)
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


class RealHerdr:
    """Shells out to `herdr` and `git`. Never used in the offline suite.

    `_run` is an attribute rather than a method call site so a test can record
    argv without executing it: what this class BUILDS is the only thing an
    offline test can check, and a wrong flag is a failure that shows up at 2am.

    That makes the offline tests weak on their own, so every command below was
    also executed against the live `herdr` on this box at least once and the
    argv here is built from `--help` plus `herdr api schema`, not from memory.
    Five of the commands the plan proposed do not exist; the tests in
    `tests/test_herdr.py` name the error each one returned.

    A task is addressed three ways and they are not interchangeable:
    `worktree create` and `pane list` take a *workspace id*, `agent start`
    takes a *pane id*, and `agent send-keys`/`agent prompt` take an *agent
    name*. The ids are learned from `worktree create --json` and cached, but
    the cache is only a shortcut -- a restarted daemon has an empty one and
    recovers the workspace id from `herdr workspace list` by checkout path,
    which is the same path-is-identity rule the module header states.
    """

    def __init__(
        self,
        worktree_root: Path,
        projects_dir: Path,
        prices: dict[str, dict[str, float]],
        repo_root: Path,
        agent_kind: str = "claude",
        agent_args: tuple[str, ...] = ("--permission-mode", "acceptEdits"),
    ) -> None:
        self.root = Path(worktree_root)
        self.projects_dir = Path(projects_dir)
        self.prices = prices
        self.repo_root = Path(repo_root)
        self.agent_kind = agent_kind
        self.agent_args = tuple(agent_args)
        self.worktrees: dict[int, Path] = {}
        self.workspaces: dict[int, str] = {}
        self.panes: dict[int, str] = {}
        self._run = self._subprocess

    def _subprocess(self, *args: str) -> str:
        try:
            return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()
        except subprocess.CalledProcessError as exc:
            # Same reasoning as RealGh._run and git._git: CalledProcessError
            # carries argv and exit code but not stderr, which is where the
            # reason is -- herdr's are named codes like
            # `dirty_worktree_requires_force` and `invalid_key`, and losing
            # them turns every failure into "exit 1".
            raise HerdrError(f"{' '.join(args)} failed: {(exc.stderr or '').strip()}") from exc

    def _json(self, *args: str) -> dict:
        out = self._run(*args)
        try:
            return json.loads(out)
        except ValueError as exc:
            # herdr reports some failures as JSON on stdout and others as plain
            # text with a non-zero exit ("unknown option: --cwd", exit 2). A
            # bare JSONDecodeError out of the daemon tick names neither the
            # command nor what came back.
            raise HerdrError(f"{' '.join(args)} did not return JSON: {out[:200]!r}") from exc

    # -- reading ------------------------------------------------------------

    def agents(self) -> list[AgentState]:
        raw = self._json("herdr", "api", "snapshot")
        return parse_snapshot(raw, self.root, self.projects_dir, self.prices)

    # -- worktrees ----------------------------------------------------------

    def create_worktree(self, task: int, slug: str, base: str) -> Path:
        path = self.root / f"{task}-{slug}"
        # Fetch first. Parent §8.2's fresh-base rule is this line: branching off
        # a stale local `integration` reproduces exactly the staleness the
        # integration branch exists to remove. `--base origin/<base>` and not
        # `--base <base>` for the same reason -- the fetch updates the
        # remote-tracking ref, so only that name carries what was fetched.
        self._run("git", "-C", str(self.repo_root), "fetch", "origin", base)
        created = self._json(
            "herdr",
            "worktree",
            "create",
            "--cwd",
            str(self.repo_root),
            "--branch",
            f"fleet/{task}-{slug}",
            "--base",
            f"origin/{base}",
            "--path",
            str(path),
            "--label",
            f"fleet-{task}",
            # Six unattended dispatches that each yank the operator's terminal
            # to a new workspace is a tool fighting its user.
            "--no-focus",
            # The only place herdr hands back the workspace id at creation
            # time, and that id is the only handle `worktree remove` accepts.
            "--json",
        )
        result = created.get("result") or {}
        workspace = (result.get("workspace") or {}).get("workspace_id")
        if workspace:
            self.workspaces[task] = workspace
        pane = (result.get("root_pane") or {}).get("pane_id")
        if pane:
            self.panes[task] = pane
        self.worktrees[task] = path
        return path

    def remove_worktree(self, task: int) -> None:
        """Removes the checkout. The branch survives -- verified live.

        Addressed by workspace id, not by path: `herdr worktree remove <path>`
        does not exist (it answers `unknown option: <path>` and exit 2). The id
        stays inside this class rather than in the `Herdr` protocol because the
        daemon and the executor know task numbers, `AgentState` carries no
        workspace id, and a restarted daemon would have no id to pass anyway --
        the lookup below has to exist regardless, which makes a parameter
        redundant at best and stale at worst.
        """
        workspace = self.workspaces.get(task) or self._workspace_of(task)
        if workspace is None:
            # Nothing found. Guessing an id here would remove some other
            # workspace's checkout, and someone's uncommitted work with it.
            return
        # --force because a retired agent's worktree is usually dirty -- that is
        # what being retired mid-turn means -- and herdr refuses a dirty removal
        # with `dirty_worktree_requires_force`. Without it REAP would fail on
        # exactly the worktrees it exists to clean up. `git worktree remove`
        # deletes the checkout, never the branch, so the work is still on
        # `fleet/<n>-<slug>` afterwards.
        self._run("herdr", "worktree", "remove", "--workspace", workspace, "--force", "--json")
        self.workspaces.pop(task, None)
        self.panes.pop(task, None)
        self.worktrees.pop(task, None)

    def _workspace_of(self, task: int) -> str | None:
        """A worktree this process did not create -- after a daemon restart, or
        an orphan whose task is no longer live. Found by asking herdr rather
        than from memory, for the same reason task identity is a path: memory
        does not survive a restart and the checkout does.

        `.get("worktree") or {}` and not `ws["worktree"]`: most workspaces in a
        real listing are ordinary ones with no worktree at all, and the source
        repo's own workspace (opened as a side effect of `worktree create
        --cwd`) has a checkout outside the worktree root. `task_of` rejects
        both.
        """
        listed = self._json("herdr", "workspace", "list")
        for workspace in (listed.get("result") or {}).get("workspaces", []):
            checkout = (workspace.get("worktree") or {}).get("checkout_path")
            if checkout and task_of(checkout, self.root) == task:
                return workspace.get("workspace_id")
        return None

    # -- agents -------------------------------------------------------------

    def start_agent(self, task: int, role: str, prompt: str) -> None:
        pane = self.panes.get(task) or self._pane_of(task)
        # Exported into the pane BEFORE the agent starts: the CLI reads them at
        # the moment a packet is written, and an unset FLEET_AGENT attributes
        # that packet to 'manual' -- indistinguishable from a human at a
        # keyboard, which is what makes it a silent dispatch bug.
        #
        # `pane send-text` and not `agent send-keys`: the agent family addresses
        # a *registered agent*, and before `agent start` there is none, so the
        # live CLI answers `agent_not_found` and the export never lands.
        # send-text types but does not submit, so the Enter below is what
        # actually runs the line.
        self._run(
            "herdr",
            "pane",
            "send-text",
            pane,
            f"export FLEET_ROLE={role} FLEET_AGENT=fleet-{task}",
        )
        self._run("herdr", "pane", "send-keys", pane, "Enter")
        start = [
            "herdr",
            "agent",
            "start",
            f"fleet-{task}",
            "--kind",
            self.agent_kind,
            "--pane",
            pane,
        ]
        if self.agent_args:
            # The separator is not decoration: without it herdr parses
            # `--permission-mode` as one of its own options and the start fails
            # before claude is ever launched.
            start += ["--", *self.agent_args]
        self._run(*start)
        # After the start, never before: `agent start` needs a pane sitting at
        # an interactive shell prompt, and `agent prompt` needs an agent herdr
        # has already detected. Reversed, the prompt is typed into a shell.
        self._run("herdr", "agent", "prompt", f"fleet-{task}", prompt)

    def _pane_of(self, task: int) -> str:
        """`herdr pane list --workspace <ID>`. There is no `--cwd` on `pane
        list` -- it answers `unknown option: --cwd` and exit 2 -- so a pane is
        reached through its workspace or not at all."""
        workspace = self.workspaces.get(task) or self._workspace_of(task)
        if workspace is None:
            raise HerdrError(f"task {task} has no herdr workspace")
        listed = self._json("herdr", "pane", "list", "--workspace", workspace)
        panes = (listed.get("result") or {}).get("panes", [])
        if not panes:
            raise HerdrError(f"workspace {workspace} for task {task} has no pane")
        return panes[0]["pane_id"]

    def stop_agent(self, task: int) -> None:
        """Stops the agent and leaves the worktree alone.

        Separate from remove_worktree on purpose: a released task's branch holds
        the work it did before it stalled, and fusing the two would delete a
        task's commits every time an agent was released.

        `C-c` twice, both measured rather than assumed. herdr's send-keys takes
        *named* keys: a literal `"\\x03"` is rejected with `invalid_key`, and so
        is `ctrl-c`. And one `C-c` into a live claude left it running and still
        in `herdr agent list`; the second exited it. A stop that silently did
        nothing would be worse than a failing one -- REAP writes the slot off
        while the agent keeps spending.
        """
        self._run("herdr", "agent", "send-keys", f"fleet-{task}", "C-c", "C-c")
