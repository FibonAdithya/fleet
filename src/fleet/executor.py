"""The executor seam, with the fake behind it.

Building the seam now is nearly free because decide() is already pure.
Retrofitting it after a daemon exists is not -- and the failure modes that
matter most overnight (stall, poison task, ledger drift, budget drain) are the
hardest to reproduce with real agents. Scripting them is the only way they
become tests.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from fleet.model import AgentState, CloseIssue, Effect, StartAgent, StopAgent

# `conflict` is deliberately absent. At this seam it is a dirty exit -- same
# status, same everything -- so decide() cannot distinguish the two and no
# scenario could assert anything a dirty exit does not already prove. `vanished`
# replaces it: ledger drift is the one failure the allocator reads as an
# absence, and nothing else here can produce one.
OUTCOMES = ("healthy", "clean_exit", "dirty_exit", "stall", "over_budget", "vanished")

_STATUS = {
    "healthy": "working",
    "stall": "working",
    "over_budget": "working",
    "clean_exit": "done",
    "dirty_exit": "dirty",
}

# A process that has exited is not live on the next tick.
_EXITED = frozenset({"done", "dirty"})

# Multiples of the per-task ceiling, never dollars. `3.0` exceeded the ceiling
# only because the fixture's per_task_usd happens to be 2.0; raising it in
# fleet.toml turned `over_budget` into a healthy agent with nothing to notice.
_OVER_BUDGET_MULTIPLE = 3.0
_DRIP_FRACTION = 0.01


class Executor(Protocol):
    """What decide()'s shell needs. `script`/`tick`/`packets`/`seed` are the
    fake's simulation surface and are deliberately not part of it."""

    def apply(self, effects: list[Effect]) -> None: ...
    def agents(self) -> list[AgentState]: ...


class FakeExecutor:
    """Scripted outcomes, no I/O. The only executor M2 has.

    `per_task_usd` is required rather than defaulted: a default is a second
    place the budget is written down, and the two disagree the moment
    `fleet.toml` changes.
    """

    def __init__(self, per_task_usd: float) -> None:
        self.applied: list[Effect] = []
        self.per_task_usd = per_task_usd
        self._script: dict[int, tuple[str, dict | None]] = {}
        self._live: dict[int, AgentState] = {}
        self._packets: dict[int, dict] = {}
        self._seq = 0

    def seed(self, agents: Iterable[AgentState]) -> None:
        """Scenario-only: put an agent decide() must see on the very first
        tick, before anything in this run ever started one -- a worktree
        surviving a daemon restart, or a human's `herdr agent start`.

        Bypasses `apply()`/`StartAgent` on purpose. Nothing decide() itself
        can ever emit reaches this method; only a scenario's `seed.agents`
        does (simulate.py), which is exactly the point -- an orphan is, by
        definition, an agent decide() did not create.

        `_seq` is raised to at least the seeded value so a later tick's
        default-healthy advance (`tick()` below) never hands out a
        `change_seq` lower than the one this agent already carries -- the
        same monotonic guarantee every other agent gets from `_seq` alone.
        """
        for agent in agents:
            self._live[agent.task] = agent
            self._seq = max(self._seq, agent.change_seq)

    def script(self, task: int, outcome: str, packet: dict | None = None) -> None:
        if outcome not in OUTCOMES:
            # A typo would otherwise script a healthy agent, and the scenario
            # would pass while proving nothing.
            raise ValueError(f"unknown outcome {outcome!r}; expected one of {OUTCOMES}")
        if outcome == "clean_exit" and not packet:
            raise ValueError(
                "clean_exit needs a packet: without one REAP has nothing to advance to, "
                "and a finished task looks identical to a healthy one"
            )
        if outcome != "clean_exit" and packet is not None:
            raise ValueError(f"{outcome!r} hands over no packet")
        self._script[task] = (outcome, packet)

    def apply(self, effects: list[Effect]) -> None:
        self.applied.extend(effects)
        for effect in effects:
            if isinstance(effect, StartAgent):
                self._seq += 1
                # A new attempt has produced nothing yet. Keeping the last
                # attempt's packet would advance a task on the work that failed.
                self._packets.pop(effect.task, None)
                self._live[effect.task] = AgentState(
                    task=effect.task, status="working", change_seq=self._seq, cost_usd=0.0
                )
            elif isinstance(effect, StopAgent):
                self._live.pop(effect.task, None)
                self._packets.pop(effect.task, None)
            elif isinstance(effect, CloseIssue):
                # Belt and suspenders: `Advance` always emits a `StopAgent`
                # before this, but nothing in this type guarantees a caller
                # applies them together, and a live agent left on a closed
                # issue would keep reporting cost every tick for a task that
                # is already over.
                self._live.pop(effect.task, None)
                self._packets.pop(effect.task, None)

    def tick(self) -> None:
        """Advance every live agent by the outcome scripted for THIS tick."""
        for task, agent in list(self._live.items()):
            if agent.status in _EXITED:
                # It exited on the previous tick. Left live it would hold a slot
                # forever, so a task that finished could never be replaced.
                del self._live[task]
                continue
            outcome, packet = self._script.get(task, ("healthy", None))
            if outcome == "vanished":
                del self._live[task]
                continue
            self._seq += 1
            if packet is not None:
                self._packets[task] = packet
            self._live[task] = AgentState(
                task=task,
                status=_STATUS[outcome],
                # A stalled agent is nominally running and producing nothing, so
                # its sequence must NOT move -- that is the only signal there is.
                change_seq=agent.change_seq if outcome == "stall" else self._seq,
                cost_usd=agent.cost_usd + self._cost(outcome),
            )
        # Consumed by the tick it was written for.
        self._script.clear()

    def _cost(self, outcome: str) -> float:
        if outcome == "over_budget":
            return self.per_task_usd * _OVER_BUDGET_MULTIPLE
        return self.per_task_usd * _DRIP_FRACTION

    def agents(self) -> list[AgentState]:
        return sorted(self._live.values(), key=lambda a: a.task)

    def packets(self) -> dict[int, dict]:
        """What finished agents handed over. The shell reads these off the issue
        after a clean exit; here they come straight from the script."""
        return dict(self._packets)
