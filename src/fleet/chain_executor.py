"""The executor for `chain` agents: starts, stops and reports chain runs.

chain is a separate tool that runs a build or a bug task to an approved pull
request on its own; fleet drives it through three commands of its CLI --
`chain start --fleet`, `chain abort` and `chain status --json` -- and never
through its store. This executor handles only `StartAgent` and `StopAgent`.
Ledger effects (labels, comments, assignees) stay in `HerdrExecutor.apply`; a
composite routes each effect to the executor that owns its task.

The one piece of state it keeps is which chain run is which fleet task, in a
JSON file written atomically, so a daemon restart still knows its runs.
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
from pathlib import Path

from fleet import ledger as ledger_module
from fleet import policy as policy_module
from fleet.config import FleetConfig
from fleet.gh import GhClient
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


class ChainStateError(Exception):
    """The task-map file exists but cannot be read as one."""


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
        #: task -> chain run id, mirrored to `state_path`.
        self._runs: dict[int, str] = self._load()
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
        if runs is not None:
            self._read_once = True
            self._last_runs = runs
            self._last_status = {run.id: run.status for run in runs}
        elif not self._read_once:
            return [
                AgentState(task=task, status="working", change_seq=-1)
                for task in sorted(self._runs)
            ] + self._failed_start_agents()
        by_run = {run_id: task for task, run_id in self._runs.items()}
        out: list[AgentState] = []
        for run in self._last_runs:
            task = by_run.get(run.id)
            if task is None or run.fleet_task is None:
                continue
            if run.fleet_task.slug != self.cfg.repo_slug:
                continue
            out.append(self._agent_of(task, run))
        return out + self._failed_start_agents()

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
        # A run already mapped to this task is stopped first: a second run on
        # one task would spend twice, and the first, unmapped, would never be
        # reported or stopped again.
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
        if status is None or status in LIVE_STATUSES:
            code, _, err = self._call([self.command, "abort", run_id])
            if code != 0:
                self._record(f"task {task}: could not abort run {run_id}: exit {code}: {err}")
        del self._runs[task]
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

    def _load(self) -> dict[int, str]:
        """The task map. A missing file is an empty map; an unreadable one
        raises ChainStateError naming the file, because an empty map would
        orphan every run it named, and those runs keep spending."""
        if not self.state_path.exists():
            return {}
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
            return out
        except (ValueError, KeyError, TypeError) as exc:
            raise ChainStateError(
                f"chain task map {self.state_path} is unreadable ({exc!r}); "
                "fix or remove it by hand -- an empty map would orphan live runs"
            ) from exc

    def _save(self) -> None:
        """Write the task map atomically: a temp file in the same directory,
        then a rename, so a crash mid-write leaves the old map whole."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"runs": {str(task): run_id for task, run_id in sorted(self._runs.items())}}
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
