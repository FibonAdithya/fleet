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
import os
import re
import tempfile
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


def _run_of(raw: dict) -> ChainRun:
    task = raw["fleet_task"]
    return ChainRun(
        id=raw["id"],
        fleet_task=(
            None
            if task is None
            else FleetTask(slug=task["slug"], number=task["number"], kind=task["kind"])
        ),
        status=raw["status"],
        stage=raw["stage"],
        run_spent_usd=float(raw["run_spent_usd"]),
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        progress=int(raw["progress"]),
        output_bytes=int(raw["output_bytes"]),
        paused_until=raw["paused_until"],
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
    ) -> None:
        self.cfg = cfg
        self.gh = gh
        self.command = command
        self.run_cli = run_cli
        self.state_path = state_path
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
        as ledger drift in one tick.
        """
        runs = self._read_status()
        if runs is not None:
            self._last_runs = runs
            self._last_status = {run.id: run.status for run in runs}
        by_run = {run_id: task for task, run_id in self._runs.items()}
        out: list[AgentState] = []
        for run in self._last_runs:
            task = by_run.get(run.id)
            if task is None or run.fleet_task is None:
                continue
            if run.fleet_task.slug != self.cfg.repo_slug:
                continue
            out.append(self._agent_of(task, run))
        out.extend(
            AgentState(task=task, status="dirty", change_seq=0)
            for task in sorted(self._failed_start)
        )
        return out

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
            if run.status == "waiting":
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
            paused=run.status == "paused",
        )

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
        raises, because guessing would orphan every run it named."""
        if not self.state_path.exists():
            return {}
        doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        return {int(task): str(run_id) for task, run_id in doc["runs"].items()}

    def _save(self) -> None:
        """Write the task map atomically: a temp file in the same directory,
        then a rename, so a crash mid-write leaves the old map whole."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"runs": {str(task): run_id for task, run_id in sorted(self._runs.items())}}
        fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, prefix=".chain-runs-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, indent=2)
            os.replace(tmp, self.state_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
