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

    `task`, `cost_usd`, and `model` are supplied by the shell: the herdr
    snapshot carries none of them. `change_seq` is herdr's `state_change_seq`,
    a monotonic counter that moves only when the agent's status changes.
    `transcript_bytes` is the combined size of the agent's transcript and its
    subagents' transcripts, 0 when none is found. Stall detection reads both:
    an agent is stalled only when neither has moved. The sequence alone holds
    still for the whole of one long turn (issue #10), and herdr's `revision`
    was measured moving on redraws while an agent sat blocked in a tool call,
    so it would hide a hung agent.

    `model` is the single model the transcript named, or None when the
    transcript named none (no transcript written yet, or no usage recorded --
    ordinary in the seconds after a dispatch) or more than one (see
    `MULTIPLE_MODELS`). A later task compares this against the declared model;
    that check deliberately ignores None, so a transcript not yet written must
    not read as a mismatch.

    A departed agent has no status of its own: it is represented by its absence
    from `agents()`, not by a value here.
    """

    task: int
    status: str  # "working" | "done" | "dirty"
    change_seq: int
    cost_usd: float = 0.0
    model: str | None = None
    transcript_bytes: int = 0


#: `AgentState.model` when a transcript named more than one model carrying
#: tokens. Distinct from None, which means "no transcript yet" or "no usage
#: recorded yet". A declared model can never equal this string, so the
#: mismatch check a later task adds escalates it without needing a special
#: case -- collapsing this into None would let a mid-session model
#: substitution pass the check vacuously.
MULTIPLE_MODELS = "<multiple>"


@dataclass(frozen=True)
class Observation:
    """When a task's agent was last seen to make progress: its sequence or
    its transcript size moved."""

    change_seq: int
    since: datetime
    transcript_bytes: int = 0


@dataclass(frozen=True)
class FleetState:
    """The fold's carried memory. Derived and disposable: task state lives in
    labels and ownership lives in the assignee, both on GitHub. Losing this
    costs one thing -- idle timers restart, so a stall in progress is noticed up
    to stall_minutes late. That is the whole cost of a daemon restart, which is
    what keeps parent §9's promise true rather than aspirational."""

    seen: tuple[tuple[int, Observation], ...] = ()
    spent_usd: float = 0.0
    # task -> the state it was CONTINUED in. The one thing that tells a stage's
    # own finished agent from the previous stage's: we started this one, for
    # this state. Disposable like `seen` -- after a restart it is empty, every
    # done agent reads as a handoff, and the fleet spends one extra continuation
    # per live task before converging. That is the whole cost, and it is
    # bounded; a durable counter would make FleetState authoritative, which
    # parent §9 forbids.
    dispatched: tuple[tuple[int, str], ...] = ()
    # task -> the cumulative `AgentState.cost_usd` already added to spent_usd
    # for the agent currently on that task. `cost_usd` is cumulative per agent
    # and a reaping intent can repeat: when a stop fails for a reason outside
    # AGENT_ABSENT_CODES the executor leaves the agent alive and the orphan pass
    # re-emits Retire every tick, so without this the same $2 was charged every
    # tick and drained a $25 fleet in thirteen -- with the run log carrying the
    # inflation into any restart.
    #
    # A high-water mark rather than a flag, so an agent that is still running
    # and still spending is charged the increment. Dropped when a Dispatch or
    # Continue starts a NEW agent on the task: that agent's cost_usd restarts at
    # zero, and keeping the old mark would net its whole spend off to nothing.
    #
    # Disposable like the two above. After a restart it is empty, so an agent
    # caught mid-stubborn-stop can be charged once more -- bounded by one
    # duplicate per restart per stuck agent, against a per-tick leak.
    charged: tuple[tuple[int, float], ...] = ()

    def observation(self, task: int) -> Observation | None:
        return dict(self.seen).get(task)

    def dispatched_in(self, task: int) -> str | None:
        return dict(self.dispatched).get(task)


@dataclass(frozen=True)
class FleetSnapshot:
    """One completed tick, rendered. Derived and disposable.

    Published by swapping a single reference, so the socket thread can never
    observe a half-written tick and needs no lock. Nothing authoritative lives
    here: losing it costs a client one refresh.

    `failures` is the night's swallowed cleanup errors, bounded and carried
    forward rather than reset each tick -- a worktree that could not be removed
    stays un-removed, so the fact stays true until a human acts on it.

    `spent_usd` is what the daemon believes it has spent tonight, which after a
    restart is `runlog.spent_from_log` for this run id. That figure counts an
    entry with no `cost_usd` key as zero, so a log carried over from a fleet
    older than Task 14 makes this a silent underestimate -- see Daemon.
    """

    run_id: str
    tick: int
    at: str
    # `hash=False` for the same reason `Task.packet` carries it: a frozen
    # dataclass hashes every field it compares, and these rows are dicts, so
    # without it `hash(snapshot)` raises TypeError -- and every set or dict
    # keyed by a snapshot breaks. Rendered rows are dicts on purpose: they are
    # `to_json`'s payload, and re-typing them as a frozen record would mean
    # maintaining a second schema for a value that exists to be serialised.
    rows: tuple[dict, ...] = field(hash=False)
    spent_usd: float
    fleet_usd: float
    free_slots: int
    held_areas: tuple[str, ...]
    draining: bool = False
    drain_reason: str = ""
    failures: tuple[str, ...] = ()

    def to_json(self) -> dict:
        """Plain JSON types only. Task 16 serves this over a socket, where a
        tuple or a Path is a TypeError inside a thread nothing is watching."""
        return {
            "run_id": self.run_id,
            "tick": self.tick,
            "at": self.at,
            "rows": [dict(row) for row in self.rows],
            "spent_usd": self.spent_usd,
            "fleet_usd": self.fleet_usd,
            "free_slots": self.free_slots,
            "held_areas": list(self.held_areas),
            "draining": self.draining,
            "drain_reason": self.drain_reason,
            "failures": list(self.failures),
        }


@dataclass(frozen=True)
class AgentSpec:
    """How to launch one agent CLI. Mechanics, not policy.

    `model_flag` is a separate field rather than a placeholder inside `args` so
    the launcher can always append `[model_flag, model]`. A placeholder someone
    forgets to include fails by running the operator's default model with no
    error at all.
    """

    kind: str
    model_flag: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    """The parsed policy layer. `integration_branch` has no default on purpose:
    a Policy built without it fails at construction rather than dispatching every
    agent off a branch named "integration" in a repo that calls it something
    else -- a mistake no test notices, because the fixture happens to agree."""

    areas: tuple[tuple[str, str], ...]  # (glob, area)
    routes: tuple[tuple[str, tuple[str, ...]], ...]  # (class, role sequence)
    agents: tuple[tuple[str, str, str], ...]  # (role, agent kind, model)
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

    def agent_for(self, role: str) -> tuple[str, str] | None:
        """(agent kind, model) for a role, or None when the policy has none."""
        for name, kind, model in self.agents:
            if name == role:
                return kind, model
        return None


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
class Continue:
    """Start the next stage's agent on a task that is already live.

    Not a Dispatch. `expand(Dispatch)` relabels to `fleet:claimed`, which for a
    task at REVIEW destroys the state it was dispatched FOR: `fleet verdict`
    would have no REVIEW to leave, `claimed` is in no stage table, and any later
    route lookup resolves to route[0] -- the implementer, redoing reviewed work.

    No attempt field either. A continuation is the next stage of one try, not a
    second try, and stamping an attempt here would push a healthy three-stage
    task to its cap before the integrator ever ran.
    """

    task: int
    role: str
    base: str


@dataclass(frozen=True)
class Release:
    task: int
    reason: str
    attempt: int


@dataclass(frozen=True)
class Rebase:
    """An integrator handed this task back to be re-based on a moved integration.

    Distinct from Release for two mechanical reasons. `expand` has to write
    `fleet:rebase-N` and only the intent can carry that number, because `expand`
    never sees the task. And `simulate.render_intent` prints every field in
    declaration order, so a fourth field on Release would rewrite the expected
    output of every scenario that asserts one.

    It costs no attempt on purpose -- a conflict is not a failed try -- which is
    why it needs a counter of its own. Without one it loops: conflict, hand back,
    re-dispatch, conflict, all night, on a counter that never moves.
    """

    task: int
    rebase: int


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
class Retire:
    """Stop an agent that has already written its own state transition.

    An implementer ends its stage by running `fleet handoff --to review`, which
    moves the label itself, and then exits. There is nothing to advance -- the
    move already happened -- and nothing to release, because the task did not
    fail. Without this intent the exited agent stays in herdr's snapshot, keeps
    `_dispatch`'s `busy` set counting it, and holds the slot for the night.

    No state field on purpose: this intent must not be able to express a
    relabel, because the label is the one thing the agent got right.
    """

    task: int


@dataclass(frozen=True)
class Close:
    """The work is over and the row still says OPEN. End it.

    Spec §6 makes reaching DONE close the issue, and `expand(Advance(to="done"))`
    does exactly that -- but nothing in the real shell emits that Advance. An
    integrator reaches DONE by running `fleet handoff --to done`, and
    `commands._post` moves the label itself, so the allocator's next tick reads
    a row already at `fleet:done`. That state is not in `decide._LIVE`, so the
    row loop skipped it entirely; the orphan pass then emitted `Retire`, which
    deliberately expresses no state change and carries no close. The row stayed
    OPEN for the rest of the night.

    Distinct from `Retire` rather than a field on it, because `Retire`'s value
    is precisely that it CANNOT express a state change -- an agent that wrote
    its own transition must not have it rewritten. Bolting a close on would
    take that property away from every use of it.

    Distinct from `Advance(to="done")` too, and for the opposite reason: there
    is no relabel to make. The label is already `fleet:done` and the agent is
    the one that put it there.

    No state field, for the same reason `Retire` has none: this intent closes a
    row that has already chosen its terminal label, and closing is the only
    thing it may do. Emitted for as long as the row reads OPEN at `fleet:done`,
    so a close that does not land is retried on the next tick and one that does
    is never repeated -- a closed row falls to the closed-row rule above it,
    which produces nothing at all once its agent is gone.
    """

    task: int


@dataclass(frozen=True)
class Drain:
    reason: str


Intent = (
    Dispatch | Continue | Release | Rebase | Escalate | Unblock | Advance | Retire | Close | Drain
)


# ---------------------------------------------------------------- effects


@dataclass(frozen=True)
class SetLabels:
    """One gh edit, carrying both halves. A relabel is two gh calls, and
    removing before adding lets a failure between them strand the task with no
    state label at all, invisible to every label query. One effect, one call."""

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


@dataclass(frozen=True)
class CloseIssue:
    task: int


Effect = SetLabels | Assign | StartAgent | StopAgent | CloseIssue
