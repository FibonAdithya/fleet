"""The tick loop. A thin imperative shell around an already-tested core.

Holds no authoritative state. Task state lives in labels, ownership in the
assignee, and the night's spend is rebuilt from the run log -- so a restart costs
one thing: idle timers start again, and a stall in progress is noticed up to
stall_minutes late.

No clock of its own. `tick` takes `now`, and `run` takes the function that
produces it, so every test states the time it means instead of sleeping until
the box agrees. A test that sleeps to synchronise is a flake waiting for a
loaded machine, and this is the layer where that temptation lives.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone

from fleet import runlog as runlog_module
from fleet.config import FleetConfig
from fleet.decide import REAPING, decide
from fleet.expand import expand
from fleet.gh import GhClient
from fleet.model import (
    AgentState,
    Continue,
    Dispatch,
    Drain,
    Effect,
    FleetSnapshot,
    FleetState,
    Intent,
    Policy,
)
from fleet.reader import read_ledger
from fleet.runlog import RunLog

_LOG = logging.getLogger(__name__)

# The states that hold a slot and an area. `decide._LIVE`, restated for the
# render rather than imported: this is a presentation question -- which rows a
# 3am glance counts as "in flight" -- and importing the allocator's private name
# would make an editorial change to the status output look like a policy change.
_LIVE_STATES = ("claimed", "review", "integrating")

# Rows in TRIAGE are not the fleet's work yet: nothing dispatches them and
# nothing reaps them, so a screen full of them buries the rows that matter.
_UNRENDERED_STATES = ("triage",)

# Intents a drain drops. Both, not just Dispatch: a Continue starts an agent
# exactly as a Dispatch does, and `decide._dispatch`'s own budget drain returns
# `[Drain]` from before either pass runs. A drain that dropped only Dispatch
# would let the whole review-and-integrate half of the fleet keep starting
# agents against a ceiling that has stopped measuring.
_HALTED_BY_DRAIN = (Dispatch, Continue)

# Dollars, for display only. `cost.py` rounds nothing on purpose -- the ceiling
# compares raw sums -- so the rounding lives here, at the edge, where it changes
# what an operator reads and nothing else.
_CENTS = 4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def stop_on_signals(stop: threading.Event, signums=(signal.SIGINT, signal.SIGTERM)):
    """Set `stop` on SIGINT and SIGTERM, and put the old handlers back after.

    Both signals, because a supervisor stopping the daemon must get the same
    clean shutdown a Ctrl-C gives: the tick in flight finishes, its effects
    land, and the socket file is removed. Exiting mid-tick instead leaves a task
    labelled `fleet:claimed` with no agent, which the next run's REAP clears at
    the cost of an attempt.

    Setting an event rather than exiting is what keeps a graceful stop
    distinguishable from a crash -- and SIGKILL, which cannot be handled, is the
    crash. That is deliberate: the restart path has to be exercised by the real
    thing, and what makes it survivable is that spend is rebuilt from the run
    log rather than handed over at shutdown.

    Restored on the way out, because these are installed for the life of a run,
    not the life of the process: a handler left behind sets an event nobody is
    waiting on, and the interpreter's own SIGINT never fires again.

    Only the main thread may install a handler; `signal.signal` raises
    ValueError elsewhere, and that is left to raise. A daemon whose stop signal
    silently did nothing is the failure this exists to prevent.
    """
    previous = {}
    try:
        for signum in signums:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stop.set())
        yield stop
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


class Daemon:
    """One tick, repeated. Everything durable is on GitHub or in the run log.

    The executor it is given must answer `apply`, `agents` and `unpriced`.
    `unpriced` is not on M2's `Executor` Protocol because M2's FakeExecutor
    predates prices; requiring it here rather than probing for it is deliberate,
    since an executor that could not report an unpriced model would run a whole
    night with the ceiling switched off and nothing to say so.
    """

    #: The night's swallowed failures, bounded. Two sources, one record: the
    #: HerdrErrors the executor kept rather than raised, and the ticks that died
    #: outright. A herdr that fails every removal appends once per task per
    #: tick, all night, so the bound is not decoration.
    FAILURE_LIMIT = 64

    def __init__(
        self,
        cfg: FleetConfig,
        policy: Policy,
        gh: GhClient,
        executor,
        runlog: RunLog,
        run_id: str,
    ) -> None:
        self.cfg = cfg
        self.policy = policy
        self.gh = gh
        self.executor = executor
        self.runlog = runlog
        self.run_id = run_id
        self.tick_number = 0
        self.snapshot: FleetSnapshot | None = None
        self.failures: deque[str] = deque(maxlen=self.FAILURE_LIMIT)
        # The restart's whole memory. Scoped to this run id because one log file
        # holds several nights, and summing all of them starts tonight drained.
        #
        # Known limitation, inherited from `spent_from_log`: an entry written
        # before Task 14 carries no `cost_usd` key and counts as zero. That can
        # only understate tonight's spend if a run id is reused across a fleet
        # upgrade, since `fleet run` mints a fresh one per start -- but it is an
        # underestimate, not a measurement, and nothing downstream can tell.
        self.state = FleetState(spent_usd=runlog_module.spent_from_log(runlog.path, run_id=run_id))

    # -- one tick -----------------------------------------------------------

    def tick(self, now: datetime) -> FleetSnapshot:
        """Read, decide, log, apply, render. Raises what it cannot handle.

        The order of the last three is not arbitrary. The log is written first
        and `self.state` advanced with it, so the two always agree: a tick that
        dies in `apply` has already recorded the spend it committed to, and a
        restart resumes with the same ceiling rather than a refunded one. The
        cost of that choice is an `outcome: applied` that overstates what landed
        when the executor itself raises -- an overstated log and an honest
        budget, rather than the reverse.
        """
        self.tick_number += 1
        ledger = read_ledger(self.gh)
        agents = self.executor.agents()
        # After the read, never before. `RealHerdr.unpriced` reports what the
        # LAST snapshot could not price, so asking first answers for the
        # previous tick: tick 1 would never drain, and every drain afterwards
        # would be armed by data one tick stale -- including the tick that
        # lifts it.
        unpriced = sorted(self.executor.unpriced())
        intents, state = decide(self.state, ledger, self.policy, agents, now)

        if unpriced:
            # Drain, do not kill. Dropping the starts stops new spend; the reap
            # is kept because a stalled agent still needs noticing while a human
            # fixes the price table. Pricing the unknown model at zero instead
            # would run the whole night with no ceiling at all.
            #
            # Filtered out of `decide`'s output rather than fed back into it,
            # because `decide` is pure and knows nothing about a price table.
            # Safe against the state that came back with them: `spent_usd` and
            # `seen` are computed from REAPING intents only, and `dispatched`
            # records the state a Continue was started in -- an entry for a
            # Continue dropped here is re-set to the same value on the tick the
            # drain lifts, and is never read while the task has no agent.
            intents = [i for i in intents if not isinstance(i, _HALTED_BY_DRAIN)]
            intents.append(Drain(reason=f"unpriced_model: {'; '.join(unpriced)}"))

        self._log(intents, agents, now)
        self.state = state
        effects: list[Effect] = []
        for intent in intents:
            effects += expand(intent, self.policy)
        self.executor.apply(effects)

        self._drain_failures()
        self.snapshot = self._render(ledger, agents, now, intents)
        return self.snapshot

    def _log(self, intents: list[Intent], agents: list[AgentState], now: datetime) -> None:
        """Every intent, with the cost it finalised.

        Charged per TASK, not per intent, exactly as `decide._final_cost` does:
        it sums over `_stopped(intents)`, a set. One task can draw two REAPING
        intents in one tick -- a `spec` row at REVIEW with a finished agent gets
        Retire from `_reap` and Escalate(unroutable_stage) from `_unroutable` --
        and a log that charged both would record twice what the fleet spent.
        The number this file writes is the number a restart reads back, so a
        double charge there drains a fleet that spent half its budget.
        """
        cost_of = {a.task: a.cost_usd for a in agents}
        charged: set[int] = set()
        for intent in intents:
            cost = 0.0
            if isinstance(intent, REAPING) and intent.task not in charged:
                # Only a reaped agent's cost is final. `AgentState.cost_usd` is
                # cumulative, so charging a live agent re-adds the same dollars
                # every tick.
                cost = cost_of.get(intent.task, 0.0)
                charged.add(intent.task)
            self.runlog.append(
                tick=self.tick_number, at=now, intent=intent, outcome="applied", cost_usd=cost
            )

    def _drain_failures(self) -> None:
        """Take the executor's swallowed HerdrErrors into the one record.

        Drained, not copied. The executor keeps them rather than raising because
        one task that cannot be cleaned up is not worth every other task's
        effects, but a deque nobody empties is a second, silent copy of a fact
        the operator never sees -- and it would report the same failure in every
        snapshot for the rest of the night. They are not re-logged here either:
        the executor already logged each one when it swallowed it, and a second
        log line is the same duplicate in a different place.

        `self.executor.failures` is required, not probed for with `getattr`: an
        executor that swallowed HerdrErrors into somewhere this cannot reach
        would make a fleet-wide herdr outage look like a quiet night, which is
        the exact failure the deque exists to prevent.
        """
        while self.executor.failures:
            self.failures.append(self.executor.failures.popleft())

    def _render(self, ledger, agents, now, intents) -> FleetSnapshot:
        by_task = {a.task: a for a in agents}
        live = [t for t in ledger if t.state in _LIVE_STATES and t.is_open]
        rows = tuple(
            {
                "task": t.number,
                "title": t.title,
                "state": t.state,
                "area": t.area,
                "class": t.klass,
                "attempt": t.attempts,
                "rebases": t.rebases,
                "owner": t.assignee,
                "agent": by_task[t.number].status if t.number in by_task else None,
                "cost_usd": round(by_task[t.number].cost_usd, _CENTS)
                if t.number in by_task
                else 0.0,
            }
            for t in sorted(ledger, key=lambda t: t.number)
            if t.is_open and t.state not in _UNRENDERED_STATES
        )
        # Every reason, joined. Two independent reasons to stop spending are two
        # facts, and a reader shown only the first fixes the wrong one at 3am.
        reasons = [i.reason for i in intents if isinstance(i, Drain)]
        return FleetSnapshot(
            run_id=self.run_id,
            tick=self.tick_number,
            at=now.isoformat(),
            rows=rows,
            spent_usd=round(self.state.spent_usd, _CENTS),
            fleet_usd=self.policy.fleet_usd,
            # The same count `decide._dispatch` makes: a running agent occupies
            # a slot whatever its row says, so the two sources of truth are
            # unioned rather than trusted separately.
            free_slots=max(0, self.policy.slots - len({t.number for t in live} | set(by_task))),
            held_areas=tuple(sorted({t.area for t in live if t.area})),
            # The Drain intent IS the record that spending has stopped -- an
            # unpriced model always appends one -- so this reads the intents
            # rather than the price fact a second time. Two sources for one
            # flag is one source that can be wrong.
            draining=bool(reasons),
            drain_reason="; ".join(reasons),
            failures=tuple(self.failures),
        )

    # -- the loop -----------------------------------------------------------

    def run(
        self,
        poll_seconds: float,
        stop: threading.Event,
        publish: Callable[[FleetSnapshot], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Ticks until `stop` is set.

        An event rather than a signal handler or a kill: a graceful stop has to
        be distinguishable from the crash the restart test needs to simulate,
        and `stop.wait` also makes the sleep interruptible so a shutdown does
        not block for a whole poll interval. `stop_on_signals` is what connects
        SIGTERM to it.

        A tick that raises costs one tick, not the run. A GitHub outage lasts
        minutes and the night lasts hours; the next tick reads the ledger again,
        which is the whole of the recovery. What survives it: the run keeps
        going, `self.state` and the run log stay in agreement, and the last good
        snapshot stays published -- stale by a known number of ticks, which a
        reader can weigh, rather than `None`, which they cannot. What does not:
        `BaseException`. A KeyboardInterrupt is not a failing tick, and a loop
        that swallowed one would make the daemon unkillable from the terminal
        that started it.

        The failing tick still waits out the poll interval before the next one,
        so a permanently broken collaborator costs one call per interval rather
        than a hot loop against a service that is already unwell.
        """
        clock = clock or _utcnow
        while not stop.is_set():
            try:
                snapshot = self.tick(clock())
            except Exception as exc:  # noqa: BLE001 -- see the docstring
                _LOG.exception("tick %s failed", self.tick_number)
                self.failures.append(f"tick {self.tick_number} failed: {exc}")
            else:
                if publish is not None:
                    publish(snapshot)
            stop.wait(poll_seconds)
