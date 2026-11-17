"""One Executor over two: herdr for agents fleet launches, chain for chain runs.

The daemon holds one executor and asks it for `apply`, `agents`, `unpriced` and
`failures`. This class answers each by routing to the executor that owns the
effect or the task, so `Daemon` and `decide` stay unaware that chain exists.

Routing:

* `StartAgent` goes by the kind of the role it starts (`Policy.agent_for`):
  kind `chain` to `ChainExecutor`, every other kind to `HerdrExecutor`.
* `StopAgent` goes to `ChainExecutor` when it owns the task (a mapped run or a
  failed start, `ChainExecutor.tasks`), else to `HerdrExecutor`. By owner and
  not by role, because a stop names no role, and an agent left over from before
  a route changed is stopped by whichever executor started it.
* Every other effect (labels, comments, assignees, closes) goes to
  `HerdrExecutor.apply`, once. `ChainExecutor.apply` refuses them.

Effects are applied one at a time in batch order, not grouped by executor: a
task's claim (`SetLabels`) precedes its start, and `ChainExecutor._start` reads
the class off the labels that claim left.
"""

from __future__ import annotations

import logging
from collections import deque

from fleet.chain_executor import ChainExecutor
from fleet.config import CHAIN_KIND
from fleet.herdr_executor import HerdrExecutor
from fleet.model import AgentState, Effect, Policy, StartAgent, StopAgent

_LOG = logging.getLogger(__name__)


class CompositeExecutor:
    """Implements the daemon's executor seam over `HerdrExecutor` and an
    optional `ChainExecutor` (None when no route names a chain role)."""

    def __init__(self, herdr: HerdrExecutor, chain: ChainExecutor | None, policy: Policy) -> None:
        self.herdr = herdr
        self.chain = chain
        self.policy = policy
        #: One deque for both executors. `Daemon._drain_failures` pops this
        #: until it is empty; a property that merged two deques on each read
        #: would hand it a fresh copy every time and never empty.
        self.failures: deque[str] = herdr.failures
        if chain is not None:
            chain.failures = herdr.failures
        #: Tasks currently reported by both executors, so a duplicate that
        #: persists is recorded once rather than once per tick.
        self._duplicates: set[int] = set()

    @property
    def owner(self) -> str | None:
        return self.herdr.owner

    def apply(self, effects: list[Effect]) -> None:
        for effect in effects:
            self._target(effect).apply([effect])

    def _target(self, effect: Effect) -> HerdrExecutor | ChainExecutor:
        if isinstance(effect, StartAgent):
            declared = self.policy.agent_for(effect.role)
            if declared is not None and declared[0] == CHAIN_KIND:
                if self.chain is None:
                    # Wiring builds a ChainExecutor whenever a route names a
                    # chain role, so this is a defect, identical for every
                    # such task. herdr would start a claude pane for it.
                    raise ValueError(
                        f"task {effect.task}: role {effect.role!r} runs on kind "
                        f"{CHAIN_KIND!r}, and this fleet has no chain executor"
                    )
                return self.chain
            return self.herdr
        if isinstance(effect, StopAgent):
            if self.chain is not None and effect.task in self.chain.tasks():
                return self.chain
            return self.herdr
        return self.herdr

    def agents(self) -> list[AgentState]:
        """Both executors' agents, sorted by task.

        A task both report is a defect somewhere -- a herdr worktree left on a
        row that chain now runs, say. decide keys agents by task, so passing
        both through would let list order pick one in silence. chain's is
        kept, because a StopAgent for a task chain owns goes to chain: the
        Retire or Release that stops it reaches the agent decide saw. herdr's
        shows on the tick after chain's has gone, and is reaped then; until
        then its cost is not seen. Recorded once per duplicate.
        """
        herdr_agents = self.herdr.agents()
        if self.chain is None:
            return herdr_agents
        chain_agents = self.chain.agents()
        chain_tasks = {agent.task for agent in chain_agents}
        both = {agent.task for agent in herdr_agents} & chain_tasks
        for task in sorted(both - self._duplicates):
            message = (
                f"task {task}: reported by both herdr and chain; "
                "using chain's; herdr's is reported once chain's has gone"
            )
            _LOG.warning("%s", message)
            self.failures.append(message)
        self._duplicates = both
        kept = [agent for agent in herdr_agents if agent.task not in chain_tasks]
        return sorted(kept + chain_agents, key=lambda agent: agent.task)

    def unpriced(self) -> set[str]:
        """herdr's unpriced models; chain prices its own runs."""
        out = set(self.herdr.unpriced())
        if self.chain is not None:
            out |= self.chain.unpriced()
        return out
