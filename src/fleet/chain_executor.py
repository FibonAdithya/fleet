"""The executor for `chain` agents: starts, stops and reports chain runs.

chain is a separate tool that runs a build or a bug task to an approved pull
request on its own; fleet drives it through three commands of its CLI --
`chain start --fleet`, `chain abort` and `chain status --json` -- and never
through its store. This executor handles only `StartAgent` and `StopAgent`.
Ledger effects (labels, comments, assignees) stay in `HerdrExecutor.apply`; a
composite routes each effect to the executor that owns its task.

The state it keeps -- which chain run is which fleet task, and which of a
finished run's ledger writes have landed -- is a JSON file written atomically,
so a daemon restart still knows its runs and never writes an outcome twice.

A run that ends with an outcome (approved, escalated, not reproduced,
duplicate) is turned into the packets a fleet stage would have written: a
handoff and an approved verdict, an escalation to the owner, or a comment and
a close. Those are the executor's only ledger writes, made from `agents()`
because that is where a run is first seen to have ended.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fleet import commands
from fleet import labels as labels_module
from fleet import ledger as ledger_module
from fleet import packet as packet_module
from fleet import policy as policy_module
from fleet.config import FleetConfig
from fleet.gh import GhClient, GhError
from fleet.ledger import LedgerError
from fleet.model import AgentState, Effect, StartAgent, StopAgent

_LOG = logging.getLogger(__name__)

#: The only status schema this module reads. chain's contract versions it, and
#: a v2 document read as v1 would misreport every run.
STATUS_SCHEMA = "chain.status.v1"

#: Run statuses that mean the run is still going. Everything else chain
#: reports (approved, ended, aborted, merged, escalated) is terminal here.
LIVE_STATUSES = frozenset({"queued", "running", "verifying", "paused", "waiting"})
#: Live statuses reported as a working agent.
_WORKING = frozenset({"queued", "running", "verifying"})

#: The classes a chain run takes, and the `--kind` each starts with.
CHAIN_CLASSES = ("build", "bug")

#: A build task's spec: a whole line of the issue body.
_SPEC_LINE = re.compile(r"Spec: (\S+)")

#: As `HerdrExecutor`'s: failures are recorded, not raised, so the record is
#: bounded.
_FAILURE_LIMIT = 64

#: What `run_cli` answers when the binary cannot be executed at all.
_EXEC_FAILED = 127

#: Seconds past `paused_until` a run may still read as paused. chain resumes a
#: paused run on its own next tick after `paused_until`; a run still paused well
#: after that is stuck, and reporting it paused would hold its slot all night
#: with the stall timer refreshed every tick.
PAUSE_GRACE_SECONDS = 600

#: The ledger writes each outcome makes, in order. Each is recorded in the state
#: file as it lands, so a retry after a partial failure (or a restart) resumes
#: at the step that failed instead of posting a second handoff.
OUTCOME_STEPS: dict[str, tuple[str, ...]] = {
    "approved": ("handoff", "verdict"),
    "escalated": ("escalate", "unassign"),
    "not_reproduced": ("comment", "done", "close"),
    "duplicate": ("comment", "done", "close"),
}

#: The writes that replace an outcome fleet could not record: an escalation
#: naming the run, so the owner sees it rather than a run that loops.
FALLBACK_STEPS: tuple[str, ...] = ("fallback", "fallback_unassign")

#: A step failure worth retrying next tick: gh or the network, or a binary that
#: could not run. Every other exception is permanent -- the run's own data
#: cannot be recorded (a null `duplicate_of`, a SHA the schema refuses) -- and a
#: retry would only loop until the stall timer released the task and started a
#: new run, throwing the finished one away.
_TRANSIENT = (GhError, OSError)

#: A git SHA as the handoff and verdict schemas accept one.
_SHA = re.compile(r"[0-9a-f]{7,40}")

#: The hidden first line of a not-reproduced or duplicate closing comment, so
#: a retry can tell that its comment already landed.
_OUTCOME_MARKER = "<!-- fleet:chain-outcome run={run_id} -->"


class ChainStateError(Exception):
    """The task-map file exists but cannot be read as one."""


@dataclass
class Outcome:
    """A finished run's ledger writes: which have landed, and the run's last
    numbers, so it can be reported without another status read."""

    outcome: str
    steps: list[str]
    cost_usd: float
    progress: int
    output_bytes: int
    #: Why the outcome could not be recorded, once a step failed permanently;
    #: from then on the record's writes are FALLBACK_STEPS.
    fallback: str | None = None

    @property
    def pending(self) -> tuple[str, ...]:
        return FALLBACK_STEPS if self.fallback is not None else OUTCOME_STEPS[self.outcome]

    @property
    def done(self) -> bool:
        return all(step in self.steps for step in self.pending)


@dataclass(frozen=True)
class FleetTask:
    """A run's `fleet_task`: the issue it was started for."""

    slug: str
    number: int
    kind: str


@dataclass(frozen=True)
class ChainRun:
    """One object of `chain status --json`'s `runs`, field for field.

    The nested documents (`escalation`, `verdict`, `pr`) stay dicts, declared
    `hash=False` for the reason `model.Task.packet` is.
    """

    id: str
    fleet_task: FleetTask | None
    status: str
    stage: str
    run_spent_usd: float
    created_at: float
    updated_at: float
    progress: int
    output_bytes: int
    paused_until: float | None
    escalation: dict | None = field(hash=False)
    outcome: str | None
    verdict: dict | None = field(hash=False)
    pr: dict | None = field(hash=False)
    branch: str | None
    base: str | None
    base_sha: str | None
    head_sha: str | None
    test_command: str | None


def _typed(raw: dict, key: str, kind: type | tuple[type, ...]):
    """`raw[key]`, refused with ValueError unless it is `kind`. bool is refused
    wherever a number is wanted: JSON `true` is an int to Python."""
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, kind):
        raise ValueError(f"run field {key!r} is {value!r}, not {kind}")
    return value


def _finite(raw: dict, key: str) -> float:
    """`raw[key]` as a finite float. nan and inf are refused: every comparison
    against nan is False, so a nan spend is never over any ceiling and a nan
    `paused_until` never ends."""
    value = float(_typed(raw, key, (int, float)))
    if not math.isfinite(value):
        raise ValueError(f"run field {key!r} is {value!r}, not a finite number")
    return value


def _run_of(raw: dict) -> ChainRun:
    task = raw["fleet_task"]
    return ChainRun(
        id=_typed(raw, "id", str),
        fleet_task=(
            None
            if task is None
            else FleetTask(
                slug=_typed(task, "slug", str),
                number=_typed(task, "number", int),
                kind=_kind_of(task),
            )
        ),
        status=_typed(raw, "status", str),
        stage=raw["stage"],
        run_spent_usd=_spend_of(raw),
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        progress=_typed(raw, "progress", int),
        output_bytes=_typed(raw, "output_bytes", int),
        paused_until=None if raw["paused_until"] is None else _finite(raw, "paused_until"),
        escalation=raw["escalation"],
        outcome=raw["outcome"],
        verdict=raw["verdict"],
        pr=raw["pr"],
        branch=raw["branch"],
        base=raw["base"],
        base_sha=raw["base_sha"],
        head_sha=raw["head_sha"],
        test_command=raw["test_command"],
    )


def _kind_of(task: dict) -> str:
    kind = task["kind"]
    if kind not in CHAIN_CLASSES:
        raise ValueError(f"fleet_task kind is {kind!r}, not one of {CHAIN_CLASSES}")
    return kind


def _spend_of(raw: dict) -> float:
    spent = _finite(raw, "run_spent_usd")
    if spent < 0:
        raise ValueError(f"run_spent_usd is {spent!r}, below zero")
    return spent


def parse_status(text: str) -> list[ChainRun]:
    """Parse `chain status --json`. Strict: anything but a `chain.status.v1`
    document whose runs carry every contract field raises ValueError.

    A run missing a field is refused rather than defaulted, for the reason
    policy parsing is strict: a defaulted `progress` of 0 reads as a run that
    has stopped moving, and a defaulted `fleet_task` as a run of no repo.
    """
    doc = json.loads(text)  # JSONDecodeError is a ValueError
    if not isinstance(doc, dict) or doc.get("schema") != STATUS_SCHEMA:
        schema = doc.get("schema") if isinstance(doc, dict) else None
        raise ValueError(f"not a {STATUS_SCHEMA} document (schema {schema!r})")
    try:
        return [_run_of(raw) for raw in doc["runs"]]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed {STATUS_SCHEMA} run: {exc!r}") from exc


def answers_path(cfg: FleetConfig, task: int) -> Path:
    """Where the box writes the owner's answers for `task`:
    `<answers_dir>/<owner>__<name>/<number>.json`."""
    owner, name = cfg.repo_slug.split("/", 1)
    return cfg.answers_dir / f"{owner}__{name}" / f"{task}.json"


def spec_of(body: str) -> str | None:
    """The path on the first line of `body` that is exactly `Spec: <path>`.

    Line by line rather than one multiline regex: an issue typed in GitHub's web
    form has CRLF line ends, and `$` does not match before a `\\r`.
    """
    for line in body.splitlines():
        found = _SPEC_LINE.fullmatch(line)
        if found:
            return found.group(1)
    return None


class ChainExecutor:
    """Implements the Executor seam for chain runs."""

    def __init__(
        self,
        cfg: FleetConfig,
        gh: GhClient,
        command: str,
        run_cli: Callable[[list[str]], tuple[int, str, str]],
        state_path: Path,
        *,
        failures: deque[str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.gh = gh
        self.command = command
        self.run_cli = run_cli
        self.state_path = state_path
        #: Epoch seconds, compared with chain's `paused_until`. Injected so the
        #: tests pin both sides of the pause window without sleeping.
        self.clock = clock
        #: Failures swallowed rather than raised, newest last and bounded; the
        #: same hand-off buffer as `HerdrExecutor.failures`, and assignable so
        #: one deque can serve both executors.
        self.failures: deque[str] = (
            failures if failures is not None else deque(maxlen=_FAILURE_LIMIT)
        )
        #: task -> chain run id, and run id -> its outcome's writes, both
        #: mirrored to `state_path`.
        self._runs: dict[int, str]
        self._outcomes: dict[str, Outcome]
        self._runs, self._outcomes = self._load()
        #: Tasks whose StartAgent started nothing. Reported as dirty agents
        #: until a StopAgent arrives: a ledger-drift `Release(no_agent)` is never
        #: escalated, so a start that fails every time would otherwise loop all
        #: night, where `dirty_exit` counts toward the attempt cap. In memory
        #: only: a restart loses it and the task reads as drift once.
        self._failed_start: set[int] = set()
        #: The last good status read, and each run id's status in it.
        self._last_runs: list[ChainRun] = []
        self._last_status: dict[str, str] = {}
        #: False until a status read succeeds. Before that, `_last_runs` is not
        #: a reading at all -- see `agents`.
        self._read_once = False
        #: Run ids whose `waiting` status has been recorded, so a run that sits
        #: there records one failure rather than one per tick.
        self._waiting_reported: set[str] = set()
        self._areas: frozenset[str] | None = None

    # -- the Executor seam -------------------------------------------------------

    def apply(self, effects: list[Effect]) -> None:
        for effect in effects:
            if isinstance(effect, StartAgent):
                self._start(effect)
            elif isinstance(effect, StopAgent):
                self._stop(effect.task)
            else:
                raise ValueError(f"ChainExecutor applies only StartAgent and StopAgent: {effect!r}")

    def agents(self) -> list[AgentState]:
        """Every mapped run of this repository, plus a dirty agent per failed
        start. Called every tick.

        A failed or unparsable status call keeps the last good read: treating it
        as every chain agent vanishing at once would release every chain task
        as ledger drift in one tick, and each release's StopAgent would abort a
        live run.

        Before the first good read since construction (a restart into a chain
        outage) there is no last good read, so each mapped task is reported
        working with `change_seq = -1`. A transient failure then aborts nothing,
        and decide's stall timer still bounds an outage that lasts: the
        sequence never moves, so after `stall_minutes` the task is released
        through `_fail` like any other stall.
        """
        runs = self._read_status()
        fresh = runs is not None
        if runs is not None:
            self._read_once = True
            self._last_runs = runs
            self._last_status = {run.id: run.status for run in runs}
        elif not self._read_once:
            return (
                [
                    AgentState(task=task, status="working", change_seq=-1)
                    for task, run_id in sorted(self._runs.items())
                    if not self._outcome_done(run_id)
                ]
                + self._finished_agents(written=set())
                + self._failed_start_agents()
            )
        by_run = {run_id: task for task, run_id in self._runs.items()}
        out: list[AgentState] = []
        written: set[str] = set()
        for run in self._last_runs:
            task = by_run.get(run.id)
            if task is None or run.fleet_task is None:
                continue
            if run.fleet_task.slug != self.cfg.repo_slug:
                continue
            if self._outcome_done(run.id):
                continue  # reported by _finished_agents below
            if run.status in LIVE_STATUSES or run.outcome not in OUTCOME_STEPS:
                out.append(self._agent_of(task, run))
                continue
            # Writes only from a read made by this call: the last good list
            # after a failed read may be any number of ticks old.
            if fresh:
                self._write_outcome(task, run)
                written.add(run.id)
                if self._outcome_done(run.id):
                    continue  # complete now: _finished_agents holds it
            out.append(self._holding(task, self._outcomes.get(run.id), run))
        return out + self._finished_agents(written) + self._failed_start_agents()

    # -- outcomes ------------------------------------------------------------------
    #
    # The daemon reads the ledger, then calls agents(), so on the tick an
    # outcome is written decide() still sees the task at `claimed`. A `done`
    # agent there has no packet and falls to the stall check, and a missing one
    # is released as `no_agent`; either relabels the task `ready` over the label
    # just written. So a written run is held `working`, its sequence one past
    # chain's last so the stall timer restarts, until the issue has left
    # `claimed`. Then it is reported `done` with its cost until a StopAgent
    # arrives: decide's Retire for that row is a reaping intent, the one place
    # the run's cost is charged into the fleet's spend. A run simply dropped
    # from agents() would never be charged.

    def _outcome_done(self, run_id: str) -> bool:
        record = self._outcomes.get(run_id)
        return record is not None and record.done

    def _holding(self, task: int, record: Outcome | None, run: ChainRun | None) -> AgentState:
        """Working, with a sequence decide has not seen: no stall, no release.

        At zero cost. decide checks a working agent's cost against the ceiling
        of the role the row is owed, and a held row may already read `review`
        (a verdict that failed once) or the run may have ended above the chain
        ceiling: either would escalate or release over the outcome just
        written. The cost rides on the `done` report instead, which decide's
        Retire reaps and charges. Accepted: a held run reaped by a stall
        Release (its writes failing for `stall_minutes`) is charged as 0.
        """
        if record is not None:
            progress, size = record.progress, record.output_bytes
        else:
            assert run is not None
            progress, size = run.progress, run.output_bytes
        return AgentState(
            task=task,
            status="working",
            change_seq=progress + 1,
            cost_usd=0.0,
            model=None,
            transcript_bytes=size,
            paused=False,
        )

    def _finished_agents(self, written: set[str]) -> list[AgentState]:
        """Every mapped run whose outcome is fully written: held working on
        the tick it was written and while the issue still reads `claimed`,
        `done` after that."""
        out: list[AgentState] = []
        claimed = labels_module.state_label("claimed")
        for task, run_id in sorted(self._runs.items()):
            record = self._outcomes.get(run_id)
            if record is None or not record.done:
                continue
            held = run_id in written
            if not held:
                try:
                    held = claimed in self.gh.get_issue(task).get("labels", [])
                except GhError as exc:
                    self._record(f"task {task}: could not read the issue: {exc}")
                    held = True
            agent = self._holding(task, record, None)
            if not held:
                agent = AgentState(
                    task=task,
                    status="done",
                    change_seq=agent.change_seq,
                    cost_usd=record.cost_usd,
                    model=None,
                    transcript_bytes=agent.transcript_bytes,
                    paused=False,
                )
            out.append(agent)
        return out

    def _write_outcome(self, task: int, run: ChainRun) -> None:
        """Make the outcome's remaining writes in order, recording each as it
        lands. A failed step is recorded and the rest wait for the next tick."""
        record = self._outcomes.get(run.id)
        if record is None:
            record = Outcome(
                outcome=run.outcome or "",
                steps=[],
                cost_usd=run.run_spent_usd,
                progress=run.progress,
                output_bytes=run.output_bytes,
            )
            self._outcomes[run.id] = record
        while True:
            for step in record.pending:
                if step in record.steps:
                    continue
                try:
                    self._write_step(step, task, run, record)
                except _TRANSIENT as exc:
                    self._record(f"task {task}: chain run {run.id}: {step} failed: {exc}")
                    self._save()
                    return
                except Exception as exc:  # permanent: see _TRANSIENT
                    if record.fallback is not None:
                        # The last resort failed too; retried next tick, and
                        # bounded by the stall timer like any held run.
                        self._record(f"task {task}: chain run {run.id}: {step} failed: {exc!r}")
                        self._save()
                        return
                    record.fallback = str(exc) or type(exc).__name__
                    self._record(
                        f"task {task}: chain run {run.id}: {step} failed permanently "
                        f"({exc!r}); escalating instead"
                    )
                    self._save()
                    break  # start over on the fallback steps
                record.steps.append(step)
                self._save()
            else:
                return

    def _write_step(self, step: str, task: int, run: ChainRun, record: Outcome) -> None:
        agent = f"chain-{run.id}"
        # A packet step is a comment and then a label move (`commands._post`).
        # One whose comment landed but whose move failed, or whose success the
        # state file never recorded (a crash before `_save`), is finished by
        # moving the label rather than posting the packet a second time.
        landed = self._landed(task, step, agent)
        if landed is not None:
            commands._move_state(self.gh, task, landed)
            return
        if step == "handoff":
            commands.do_handoff(
                self.cfg, self.gh, task=task, to="review", role="chain", **self._packet(run)
            )
        elif step == "verdict":
            _, head = self._shas(run)
            commands.do_verdict(
                self.cfg,
                self.gh,
                task=task,
                verdict="approved",
                head_sha=head,
                reviewer=agent,
                role="chain",
                findings=[],
            )
        elif step == "escalate":
            found = run.escalation or {}
            text = found.get("text")
            question = text.strip() if isinstance(text, str) else ""
            commands.do_escalate(
                self.cfg,
                self.gh,
                task=task,
                question=question
                or f"chain run {run.id} escalated at stage {run.stage} without a question",
                considered=(f"chain run {run.id} ended at stage {run.stage} ({found.get('kind')})"),
                role="chain",
                **self._packet(run),
            )
        elif step == "fallback":
            base, head = self._fallback_shas(run)
            commands.do_escalate(
                self.cfg,
                self.gh,
                task=task,
                question=(
                    f"chain run {run.id} ended with outcome {record.outcome}, "
                    f"but fleet could not record it: {record.fallback}"
                ),
                considered=(
                    f"fleet wrote nothing else for this run; check chain status for run {run.id}"
                ),
                role="chain",
                agent=agent,
                run=self._now(),
                base_sha=base,
                head_sha=head,
                branch=run.branch or self.cfg.integration_branch,
                did=f"fleet could not record chain run {run.id}'s outcome",
                checks="none: fleet recorded no result for this run",
                commands=[f"{self.command} status --json"],
            )
        elif step in ("unassign", "fallback_unassign"):
            # As expand's Escalate does: the box reads an assigned
            # `fleet:human` issue as taken by the owner.
            self.gh.assign(task, None)
        elif step == "comment":
            marker = _OUTCOME_MARKER.format(run_id=run.id)
            # A comment that landed but was never recorded (a crash before
            # `_save`) carries this run's marker: do not post it again.
            comments = self.gh.get_issue(task).get("comments", [])
            if not any(body.split("\n", 1)[0] == marker for body in comments):
                self.gh.comment(task, f"{marker}\n{_verdict_text(run)}")
        elif step == "done":
            commands._move_state(self.gh, task, "done")
        elif step == "close":
            self.gh.close_issue(task)
        else:  # pragma: no cover - OUTCOME_STEPS names every step
            raise AssertionError(step)

    def _landed(self, task: int, step: str, agent: str) -> str | None:
        """The state a packet step moves to, if this run's packet for it is
        already the newest of its kind on the issue; else None."""
        target = {
            "handoff": "review",
            "escalate": "human",
            "fallback": "human",
            "verdict": "integrating",
        }.get(step)
        if target is None:
            return None
        comments = self.gh.get_issue(task).get("comments", [])
        if step == "verdict":
            found = packet_module.parse_latest(comments, "verdict")
            mine = found is not None and found.get("reviewer") == agent
        else:
            found = packet_module.parse_latest(comments, "handoff")
            sender = (found or {}).get("from") or {}
            mine = sender.get("agent") == agent and (found or {}).get("to") == target
        return target if mine else None

    def _packet(self, run: ChainRun) -> dict:
        """The handoff fields an approved or escalated run's packet shares."""
        base, head = self._shas(run)
        if run.outcome == "approved":
            did = f"chain run {run.id}: plan, audit, implement, review"
        else:
            did = f"chain run {run.id}: escalated at stage {run.stage}"
        if run.outcome == "approved":
            if not run.test_command:
                # chain's approval gate always records the test command it ran;
                # an approval without one is a contract break, not a pass.
                raise ValueError(f"chain run {run.id} was approved with no test command")
            checks = "test command passed at the reviewed SHA"
            cmds = [run.test_command]
        elif run.test_command:
            checks = "chain escalated before its tests ran to an approval"
            cmds = [run.test_command]
        else:
            # An escalation can come before any test ran. The schema wants one
            # command: the one that shows the owner this run.
            checks = f"chain escalated run {run.id} before recording a test command"
            cmds = [f"{self.command} status --json"]
        return {
            "agent": f"chain-{run.id}",
            "run": self._now(),
            "base_sha": base,
            "head_sha": head,
            "branch": run.branch or self.cfg.integration_branch,
            "did": did,
            "checks": checks,
            "commands": cmds,
        }

    def _shas(self, run: ChainRun) -> tuple[str, str]:
        """(base, head): the run's own, the base standing in for a missing
        head, and the integration branch's head for a run that failed during
        setup and has neither."""
        head = run.head_sha or run.base_sha
        if head is None:
            sha = self._integration_head()
            return sha, sha
        return run.base_sha or head, head

    def _fallback_shas(self, run: ChainRun) -> tuple[str, str]:
        """As `_shas`, but a SHA the schema would refuse counts as missing:
        the fallback escalation is the write that has to land."""
        base = run.base_sha if run.base_sha and _SHA.fullmatch(run.base_sha) else None
        head = run.head_sha if run.head_sha and _SHA.fullmatch(run.head_sha) else None
        head = head or base
        if head is None:
            sha = self._integration_head()
            return sha, sha
        return base or head, head

    def _now(self) -> str:
        """RFC 3339, from the injected clock."""
        return datetime.fromtimestamp(self.clock(), UTC).isoformat()

    def _integration_head(self) -> str:
        argv = [
            "git",
            "-C",
            str(self.cfg.root),
            "rev-parse",
            f"origin/{self.cfg.integration_branch}",
        ]
        code, out, err = self._call(argv)
        sha = out.strip()
        if code != 0 or not sha:
            # Transient: the repository, not the run, is what failed.
            raise OSError(f"git rev-parse exited {code}: {err.strip()}")
        return sha

    def _failed_start_agents(self) -> list[AgentState]:
        return [
            AgentState(task=task, status="dirty", change_seq=0)
            for task in sorted(self._failed_start)
        ]

    def unpriced(self) -> set[str]:
        """Always empty: chain prices its own runs and reports dollars."""
        return set()

    def tasks(self) -> set[int]:
        """The tasks this executor owns: mapped runs and failed starts."""
        return set(self._runs) | self._failed_start

    # -- start and stop ------------------------------------------------------------

    def _start(self, effect: StartAgent) -> None:
        """Start a chain run. Every way it can start nothing is recorded and
        reported as a dirty agent, never raised.

        It does not assign the issue, unlike `HerdrExecutor._start`: the box
        reads an assigned `fleet:human` issue as taken by the owner.
        """
        task = effect.task
        # chain has no idempotence for a repeated `chain start --fleet` on one
        # task, and a second run would spend twice. So a mapped run that may
        # still be going -- live in the last read, or not read at all yet --
        # is kept and nothing is started. Only a run known to have ended is
        # forgotten and replaced.
        run_id = self._runs.get(task)
        if run_id is not None:
            status = self._last_status.get(run_id)
            ended = run_id in self._outcomes or (status is not None and status not in LIVE_STATUSES)
            if not ended:
                self._record(
                    f"task {task}: started no chain run: run {run_id} is mapped and "
                    f"{'live' if status else 'of unknown status'} ({status}); keeping it"
                )
                return
        self._forget(task)
        issue = self.gh.get_issue(task)
        try:
            klass = ledger_module.read_one(issue, areas=self._area_names()).klass
        except LedgerError as exc:
            self._start_failed(task, f"its labels do not parse: {exc}")
            return
        if klass not in CHAIN_CLASSES:
            self._start_failed(task, f"class {klass!r} is not one chain runs")
            return
        argv = [
            self.command,
            "start",
            "--fleet",
            f"{self.cfg.repo_slug}#{task}",
            "--kind",
            klass,
            "--base",
            effect.base,
        ]
        answers = answers_path(self.cfg, task)
        if answers.is_file():
            argv += ["--answers", str(answers)]
        argv.append(str(self.cfg.root))
        if klass == "build":
            spec = spec_of(issue.get("body") or "")
            if spec is None:
                self._start_failed(task, "a build issue has no `Spec: <path>` line")
                return
            argv.append(spec)
        code, out, err = self._call(argv)
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if code != 0 or not lines:
            self._start_failed(task, f"chain start exited {code}: {err.strip() or 'no run id'}")
            return
        self._failed_start.discard(task)
        self._runs[task] = lines[-1]
        self._save()

    def _stop(self, task: int) -> None:
        """Abort the task's run and forget it either way.

        A run last seen terminal is forgotten without an abort: chain refuses to
        abort an ended run (exit 2), and there is nothing left to stop.
        """
        if task in self._failed_start:
            self._failed_start.discard(task)
            return
        self._forget(task)

    def _forget(self, task: int) -> None:
        """Abort the task's mapped run unless it is known terminal, then drop
        the mapping. An abort that fails is recorded, and the mapping still
        goes: `_stop` explains why a known-terminal run is not aborted, and a
        run whose status is unknown (no good read yet) is aborted, since it may
        be live."""
        run_id = self._runs.get(task)
        if run_id is None:
            return
        status = self._last_status.get(run_id)
        # A run with an outcome record has ended, whatever this process has
        # read: after a restart there may be no status read at all.
        ended = run_id in self._outcomes
        if not ended and (status is None or status in LIVE_STATUSES):
            code, _, err = self._call([self.command, "abort", run_id])
            if code != 0:
                self._record(f"task {task}: could not abort run {run_id}: exit {code}: {err}")
        del self._runs[task]
        self._outcomes.pop(run_id, None)
        self._save()

    # -- helpers -------------------------------------------------------------------

    def _agent_of(self, task: int, run: ChainRun) -> AgentState:
        if run.status in _WORKING or run.status == "paused":
            status = "working"
        else:
            # `waiting`, or a terminal run (Task 7 turns those into outcomes).
            status = "dirty"
            if run.status == "waiting" and run.id not in self._waiting_reported:
                self._waiting_reported.add(run.id)
                self._record(
                    f"task {task}: chain run {run.id} is waiting on its owner; "
                    "a fleet-mode run never asks"
                )
        return AgentState(
            task=task,
            status=status,
            change_seq=run.progress,
            cost_usd=run.run_spent_usd,
            model=None,
            transcript_bytes=run.output_bytes,
            paused=self._is_paused(run),
        )

    def _is_paused(self, run: ChainRun) -> bool:
        """Paused, and not long past the time chain said it would resume. A
        null `paused_until` is a pause with no end announced."""
        if run.status != "paused":
            return False
        if run.paused_until is None:
            return True
        return run.paused_until + PAUSE_GRACE_SECONDS > self.clock()

    def _read_status(self) -> list[ChainRun] | None:
        code, out, err = self._call([self.command, "status", "--json"])
        if code != 0:
            self._record(f"chain status exited {code}: {err.strip()}")
            return None
        try:
            return parse_status(out)
        except ValueError as exc:
            self._record(f"chain status output unreadable: {exc}")
            return None

    def _call(self, argv: list[str]) -> tuple[int, str, str]:
        """`run_cli`, with a binary that cannot be executed answered as a
        failed call rather than raised out of the tick."""
        try:
            return self.run_cli(argv)
        except OSError as exc:
            return (_EXEC_FAILED, "", f"cannot run {argv[0]}: {exc}")

    def _area_names(self) -> frozenset[str]:
        if self._areas is None:
            self._areas = policy_module.area_names(self.cfg)
        return self._areas

    def _start_failed(self, task: int, why: str) -> None:
        self._failed_start.add(task)
        self._record(f"task {task}: started no chain run: {why}")

    def _record(self, message: str) -> None:
        _LOG.warning("%s", message)
        self.failures.append(message)

    def _load(self) -> tuple[dict[int, str], dict[str, Outcome]]:
        """The task map and the outcome records. A missing file is empty; an
        unreadable one raises ChainStateError naming the file, because an empty
        map would orphan every run it named, and those runs keep spending, and
        empty outcome records would write every finished run's packets again."""
        if not self.state_path.exists():
            return {}, {}
        try:
            doc = json.loads(self.state_path.read_text(encoding="utf-8"))
            runs = doc["runs"]
            if not isinstance(runs, dict):
                raise TypeError(f"runs is {type(runs).__name__}, not an object")
            out: dict[int, str] = {}
            for task, run_id in runs.items():
                if not isinstance(run_id, str):
                    raise TypeError(f"run id for task {task!r} is {run_id!r}")
                out[int(task)] = run_id
            # Absent in a file written before outcomes were recorded.
            raw_outcomes = doc.get("outcomes", {})
            if not isinstance(raw_outcomes, dict):
                raise TypeError(f"outcomes is {type(raw_outcomes).__name__}, not an object")
            outcomes = {run_id: _outcome_of(raw) for run_id, raw in raw_outcomes.items()}
            return out, outcomes
        except (ValueError, KeyError, TypeError) as exc:
            raise ChainStateError(
                f"chain task map {self.state_path} is unreadable ({exc!r}); "
                "fix or remove it by hand -- an empty map would orphan live runs"
            ) from exc

    def _save(self) -> None:
        """Write the task map atomically: a temp file in the same directory,
        then a rename, so a crash mid-write leaves the old map whole."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "runs": {str(task): run_id for task, run_id in sorted(self._runs.items())},
            "outcomes": {
                run_id: {
                    "outcome": record.outcome,
                    "steps": list(record.steps),
                    "cost_usd": record.cost_usd,
                    "progress": record.progress,
                    "output_bytes": record.output_bytes,
                    "fallback": record.fallback,
                }
                for run_id, record in sorted(self._outcomes.items())
            },
        }
        fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, prefix=".chain-runs-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, indent=2)
                # The rename is atomic only over data that reached the disk; a
                # crash after an unsynced rename can leave an empty file.
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _outcome_of(raw: dict) -> Outcome:
    """One outcome record, read as strictly as a status run."""
    if not isinstance(raw, dict):
        raise TypeError(f"outcome record is {type(raw).__name__}, not an object")
    outcome = _typed(raw, "outcome", str)
    if outcome not in OUTCOME_STEPS:
        raise ValueError(f"outcome {outcome!r} is not one fleet writes")
    steps = raw["steps"]
    known = OUTCOME_STEPS[outcome] + FALLBACK_STEPS
    if not isinstance(steps, list) or not all(step in known for step in steps):
        raise ValueError(f"steps {steps!r} are not steps of {outcome!r}")
    # Absent in a record written before fallbacks existed.
    fallback = raw.get("fallback")
    if fallback is not None and not isinstance(fallback, str):
        raise ValueError(f"fallback is {fallback!r}, not a string")
    spent = _finite(raw, "cost_usd")
    if spent < 0:
        raise ValueError(f"cost_usd is {spent!r}, below zero")
    return Outcome(
        outcome=outcome,
        steps=list(steps),
        cost_usd=spent,
        progress=_typed(raw, "progress", int),
        output_bytes=_typed(raw, "output_bytes", int),
        fallback=fallback,
    )


def _verdict_text(run: ChainRun) -> str:
    """The comment a not-reproduced or duplicate bug closes with, from chain's
    `verdict` object."""
    verdict = run.verdict or {}
    if run.outcome == "duplicate":
        number = verdict.get("duplicate_of")
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(f"chain run {run.id} is a duplicate of {number!r}, not an issue")
        return f"Duplicate of #{number}\n\nchain run {run.id} found this issue already filed."
    head = f"chain run {run.id} could not reproduce this bug."
    tried = _tried_items(verdict.get("tried"))
    if not tried:
        return f"{head}\n\nchain did not say what it tried."
    return f"{head}\n\nWhat was tried:\n" + "\n".join(f"- {item}" for item in tried)


def _tried_items(tried: object) -> list[str]:
    """chain's `verdict.tried`: a list of strings, or one string from an older
    run. Anything else, and blank entries, read as nothing said."""
    if isinstance(tried, str):
        tried = [tried]
    if not isinstance(tried, list):
        return []
    return [item.strip() for item in tried if isinstance(item, str) and item.strip()]
