"""Frozen domain types. Every collection is a tuple, so a snapshot handed to a
caller cannot be edited underneath the code that produced it -- the same defect
FakeGh.get_issue was fixed for in M1."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Task:
    """One GitHub issue, as the allocator sees it.

    `packet` and `verdict` are populated only for tasks whose state carries one,
    because `gh issue list` does not fetch comment bodies and FakeGh mirrors
    that. `None` here is a fact about the query, not about the issue.

    Those two are the one exception to the tuples-everywhere rule: a handoff
    packet is a parsed JSON document, and freezing it would mean re-typing a
    schema that already has one. They are declared `hash=False` so the exception
    costs nothing -- a frozen dataclass hashes every compared field, so without
    it `hash(Task(..., packet={...}))` raises TypeError and every set or dict
    keyed by Task breaks the moment a packet arrives.
    """

    number: int
    title: str
    state: str
    area: str | None
    klass: str | None
    auto_ok: bool
    attempts: int = 0
    fix_passes: int = 0
    rebases: int = 0
    assignee: str | None = None
    is_open: bool = True
    blocked_on: int | None = None
    parent_epic: int | None = None
    packet: dict | None = field(default=None, hash=False)
    verdict: dict | None = field(default=None, hash=False)


@dataclass(frozen=True)
class AgentState:
    """A herdr agent, normalized.

    `task` and `cost_usd` are supplied by the shell: the herdr snapshot carries
    neither. `change_seq` is herdr's `state_change_seq`, a monotonic counter --
    a value that has not moved since the last tick is what stall detection reads.

    A departed agent has no status of its own: it is represented by its absence
    from `agents()`, not by a value here.
    """

    task: int
    status: str  # "working" | "done" | "dirty"
    change_seq: int
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Observation:
    """When a task's agent sequence was last seen to move."""

    change_seq: int
    since: datetime


@dataclass(frozen=True)
class FleetState:
    """The fold's carried memory. Derived and disposable: task state lives in
    labels and ownership lives in the assignee, both on GitHub. Losing this
    costs one thing -- idle timers restart, so a stall in progress is noticed up
    to stall_minutes late. That is the whole cost of a daemon restart, which is
    what keeps parent §9's promise true rather than aspirational."""

    seen: tuple[tuple[int, Observation], ...] = ()
    spent_usd: float = 0.0

    def observation(self, task: int) -> Observation | None:
        return dict(self.seen).get(task)


@dataclass(frozen=True)
class Policy:
    """The parsed policy layer. `integration_branch` has no default on purpose:
    a Policy built without it fails at construction rather than dispatching every
    agent off a branch named "integration" in a repo that calls it something
    else -- a mistake no test notices, because the fixture happens to agree."""

    areas: tuple[tuple[str, str], ...]  # (glob, area)
    routes: tuple[tuple[str, tuple[str, ...]], ...]  # (class, role sequence)
    slots: int
    stall_minutes: int
    max_attempts: int
    max_fix_passes: int
    max_rebases: int
    per_task_usd: float
    fleet_usd: float
    integration_branch: str

    def route_for(self, klass: str | None) -> tuple[str, ...]:
        return dict(self.routes).get(klass or "", ())


# ---------------------------------------------------------------- intents


@dataclass(frozen=True)
class Dispatch:
    """`attempt` is the number this dispatch will stamp on the task, computed by
    `decide()` and read back by `expand()`. Carrying it on the intent is what
    keeps one writer: with the counter defaulted in `expand` and hardcoded at the
    `Dispatch` site, the two disagreed and the label never advanced past 1.

    Field order is the render order (`simulate.render_intent` prints every field
    but `base`), so a scenario asserts `Dispatch(5, implementer, 1)`."""

    task: int
    role: str
    base: str
    attempt: int


@dataclass(frozen=True)
class Release:
    task: int
    reason: str
    attempt: int


@dataclass(frozen=True)
class Escalate:
    task: int
    reason: str


@dataclass(frozen=True)
class Unblock:
    task: int


@dataclass(frozen=True)
class Advance:
    task: int
    to: str


@dataclass(frozen=True)
class Drain:
    reason: str


Intent = Dispatch | Release | Escalate | Unblock | Advance | Drain


# ---------------------------------------------------------------- effects


@dataclass(frozen=True)
class SetLabels:
    """One gh edit, carrying both halves. `commands._post` removes all eight
    state labels and then adds one; a failure between the two strands the task
    with no state label at all, invisible to every label query. One effect, one
    call."""

    task: int
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assign:
    task: int
    owner: str | None


@dataclass(frozen=True)
class StartAgent:
    task: int
    role: str
    base: str


@dataclass(frozen=True)
class StopAgent:
    task: int


Effect = SetLabels | Assign | StartAgent | StopAgent
