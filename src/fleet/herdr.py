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
    """Anything the herdr client could not do.

    `code` is herdr's own machine-readable error code when there is one --
    `agent_not_found`, `invalid_key`, `dirty_worktree_requires_force` -- and
    None for a failure that carried no JSON body. It exists because
    `_subprocess` otherwise collapses every non-zero exit into one exception,
    and some callers have to tell one failure from another: the executor
    deletes a stopped agent's worktree, and doing that after a stop that failed
    for any reason OTHER than the agent already being gone deletes a running
    agent's checkout out from under it.

    Defaulted rather than required so that `.code` is always present. An
    attribute that exists on some instances and not others is an AttributeError
    inside an except clause, which is the worst place to find one.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


#: Codes that mean "the agent this call addressed is not running". The whole
#: point of reading a code at all -- see HerdrError.
AGENT_ABSENT_CODES = frozenset({"agent_not_found"})


def error_code(stderr: str) -> str | None:
    """herdr's `{"error":{"code":...}}` out of a stderr buffer, or None.

    Not every failure is JSON: an unrecognised flag answers `unknown option:
    --cwd` and exit 2 as plain text. None for those, because inventing a code
    would let a caller act on a failure it has not actually recognised.

    The last non-empty line as well as the whole buffer, since a warning
    printed before the body is ordinary and would otherwise take the code with
    it.
    """
    for candidate in (stderr.strip(), stderr.strip().splitlines()[-1] if stderr.strip() else ""):
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            code = (payload.get("error") or {}).get("code")
            if code:
                return str(code)
    return None


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
    """In-memory, with the same surface. The only client tests use.

    Where `RealHerdr` raises, this raises too. That is not politeness: it is
    the rule from docs/m2-inputs.md §6, where four M1 defects were all cases of
    a fake being subtly *more* permissive than the real client, and every one
    of them passed the whole offline suite and failed against the live service.
    A fake must be less permissive than the real client, never more, so that a
    production failure becomes an offline one.

    The three guards below each mirror a specific `RealHerdr` failure, named at
    the guard.
    """

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
        # Real herdr fails a create onto an existing path or branch. Silently
        # overwriting the map entry would make a duplicate dispatch into an
        # already-occupied worktree look like a clean create. Removal frees the
        # task again, because it frees the path.
        if task in self.worktrees:
            raise HerdrError(f"task {task} already has a worktree at {self.worktrees[task]}")
        path = self.root / f"{task}-{slug}"
        self.worktrees[task] = path
        return path

    def remove_worktree(self, task: int) -> None:
        # Deliberately NOT guarded: `RealHerdr.remove_worktree` is a successful
        # no-op when there is nothing to remove, so raising here would make the
        # fake stricter than the real client in the one direction that is also
        # wrong -- it would reject a call the real client accepts.
        self.worktrees.pop(task, None)
        self.removed.append(task)

    def start_agent(self, task: int, role: str, prompt: str) -> None:
        # `RealHerdr.start_agent` resolves a pane through the task's workspace
        # and raises `task <n> has no herdr workspace` when there is none. The
        # ordering bug this catches -- start_agent before create_worktree --
        # would otherwise pass every offline test.
        if task not in self.worktrees:
            raise HerdrError(f"task {task} has no herdr workspace")
        self.started.append((task, role, prompt))
        self.add_agent(task, "working", change_seq=len(self.started))

    def stop_agent(self, task: int) -> None:
        # `RealHerdr.stop_agent` shells `herdr agent send-keys fleet-<task>`,
        # which answers `agent_not_found` and rc=1 for a target that is not
        # there, so it raises. A double-stop must not be silently fine here
        # when it is an exception on the box.
        #
        # With the code, because the executor branches on it: a stop that fails
        # because the agent is already gone is safe to follow with a worktree
        # removal, and any other failure is not. A fake that raised without one
        # would send every stop down the cautious path and leak a worktree per
        # task, in a way no offline test could see.
        if task not in self._agents:
            raise HerdrError(f"agent fleet-{task} not found", code="agent_not_found")
        self._agents.pop(task)
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
            #
            # The code is parsed out as well as printed. A caller branching on
            # a substring of the message would break on the first message herdr
            # rewords; the code is the part herdr means as an interface.
            stderr = (exc.stderr or "").strip()
            raise HerdrError(f"{' '.join(args)} failed: {stderr}", code=error_code(stderr)) from exc

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
        # Fetch first. Parent §8.2's fresh-base rule is this line: branching off
        # a stale local `integration` reproduces exactly the staleness the
        # integration branch exists to remove. `--base origin/<base>` and not
        # `--base <base>` for the same reason -- the fetch updates the
        # remote-tracking ref, so only that name carries what was fetched.
        self._run("git", "-C", str(self.repo_root), "fetch", "origin", base)
        existing = self._existing_branch(task)
        branch = existing or f"fleet/{task}-{slug}"
        # The checkout is named after the branch it is on, so the two cannot
        # drift apart when the issue is retitled between stages.
        path = self.root / branch.split("/", 1)[1]
        args = ["--cwd", str(self.repo_root), "--branch", branch]
        if existing is None:
            # `--base` only when the branch is new. Every stage after the first
            # arrives here on a branch that already exists -- the reviewer's
            # continuation, and a re-dispatch after a rebase handback -- and
            # re-basing it on `origin/<base>` would silently discard the commits
            # that are the entire reason the next stage was started.
            args += ["--base", f"origin/{base}"]
        args += [
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
        ]
        created = self._json("herdr", "worktree", "create", *args)
        result = created.get("result") or {}
        workspace = (result.get("workspace") or {}).get("workspace_id")
        if not workspace:
            # Not a successful create, and it must not look like one. The
            # worktree is on disk by now, and the id is the only handle that
            # removes it -- returning the path anyway would leave a checkout
            # nothing can clean up and a caller with no way to know. A herdr
            # field rename is exactly how this happens.
            raise HerdrError(
                f"herdr worktree create for task {task} returned no workspace id: {created!r}"
            )
        self.workspaces[task] = workspace
        pane = (result.get("root_pane") or {}).get("pane_id")
        if pane:
            self.panes[task] = pane
        self.worktrees[task] = path
        return path

    def _existing_branch(self, task: int) -> str | None:
        """This task's branch, found by the `fleet/<n>-` prefix. None if it has
        none yet.

        By prefix and not by the exact `fleet/<n>-<slug>` the caller just built,
        because the slug comes from the issue title and a title is mutable. A
        human sharpening the wording of an in-flight issue changes what
        `slug_of` returns; looked up by exact slug, the branch carrying the
        implementer's commits then reads as missing, the create takes the
        `--base origin/<base>` path, and the reviewer starts from an empty tree
        with the work stranded on a branch nothing checks out again. That is
        verbatim the failure the `--base` guard exists to prevent. The task
        number is the identity here, exactly as it is for the checkout path.

        `refs/heads/fleet/<n>-*` and not a bare name: a *tag* called
        `fleet/1-lid` must not answer for a branch, and the trailing dash in the
        pattern is what stops `fleet/1-*` from matching `fleet/12-lid`.

        Sorted by committer date so that two branches for one task -- two
        retitles -- resolve to the one carrying the most recent work, and
        resolve to the SAME one every tick. Alphabetical order would flip the
        next stage onto an abandoned branch on a rename.

        A `for-each-ref` that fails outright says nothing about the branch, and
        the safe reading is "not there": a first dispatch then works, and the
        only cost on a re-dispatch is a create herdr refuses -- loudly, one
        task. Read the other way, a failing git would silently drop every
        task's `--base`.
        """
        try:
            out = self._run(
                "git",
                "-C",
                str(self.repo_root),
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)",
                f"refs/heads/fleet/{task}-*",
            )
        except HerdrError:
            return None
        branches = [line.strip() for line in out.splitlines() if line.strip()]
        return branches[0] if branches else None

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
            # Two very different situations answer None here, and collapsing
            # them is what makes a leak unobservable.
            checkout = self._checkout_of(task)
            if checkout is None:
                # Nothing to remove. A silent, successful no-op -- and note
                # that guessing an id instead would remove some other
                # workspace's checkout, and someone's uncommitted work.
                return
            # A checkout that exists with no workspace to remove it through.
            # Reporting it is not clearing it: the checkout stays, the task
            # goes back to READY, and its next dispatch fails on the occupied
            # path -- every tick, forever, with no counter moving. So the
            # git-level removal this used to tell a human to run is run here.
            #
            # Confined to fleet's own worktrees by construction: `_checkout_of`
            # resolves the path through `task_of`, which rejects anything
            # outside `self.root`. --force because a retired agent's worktree
            # is usually dirty, which is what being retired mid-turn means.
            try:
                self._run(
                    "git",
                    "-C",
                    str(self.repo_root),
                    "worktree",
                    "remove",
                    "--force",
                    str(checkout),
                )
            except HerdrError as exc:
                # Nothing left to try. The next dispatch will fail on the
                # occupied path, so the message has to name the path and the
                # command -- silence would make that later failure point at the
                # wrong thing entirely.
                raise HerdrError(
                    f"task {task} has a worktree checkout at {checkout} with no herdr "
                    f"workspace, and removing it with git failed too ({exc}); remove it "
                    f"by hand (git -C {self.repo_root} worktree remove --force {checkout})",
                    code=getattr(exc, "code", None),
                ) from exc
            self.worktrees.pop(task, None)
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

    def _checkout_of(self, task: int) -> Path | None:
        """The git-level view, which `workspace list` cannot give.

        A worktree whose herdr workspace has been closed is gone from
        `workspace list` -- it is not a workspace any more -- but `herdr
        worktree list --cwd <repo> --json` still lists the checkout, with
        `open_workspace_id` simply absent. That difference is the only way to
        tell "nothing to remove" from "a checkout with nothing to remove it
        through", and it was captured live by creating a worktree and closing
        its workspace (fixtures/herdr/worktree-list-orphan.json).

        `--cwd self.repo_root` because `worktree list` is scoped to one repo:
        without it, herdr answers for whatever repo the daemon's own working
        directory happens to sit in.
        """
        listed = self._json("herdr", "worktree", "list", "--cwd", str(self.repo_root), "--json")
        for worktree in (listed.get("result") or {}).get("worktrees", []):
            path = worktree.get("path")
            if path and task_of(path, self.root) == task:
                return Path(path)
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
