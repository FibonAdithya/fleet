"""The real executor. Composes herdr, gh and the prompt layer behind M2's seam.

M2's `Executor` protocol is `apply(effects)` and `agents()`, and it does not
move: FakeExecutor and all seven scenarios stand on it, and they are M2's whole
deliverable. Everything real happens on this side of it.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque

from fleet import labels as labels_module
from fleet import packet as packet_module
from fleet import policy as policy_module
from fleet.config import FleetConfig
from fleet.gh import GhClient
from fleet.herdr import AGENT_ABSENT_CODES, Herdr, HerdrError
from fleet.model import (
    AgentState,
    Assign,
    CloseIssue,
    Comment,
    Effect,
    SetLabels,
    StartAgent,
    StopAgent,
)

_LOG = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX = 48
# A title with nothing sluggable in it -- "###", or one written entirely in a
# non-latin script -- would otherwise make the directory `<n>-` and the branch
# `fleet/<n>-`. Both are legal and both read as truncated.
_SLUG_FALLBACK = "task"
# Cleanup failures are recorded rather than raised, so the record is bounded:
# a herdr that fails every removal would otherwise append once per task per
# tick, all night, into a list nothing ever drains.
_FAILURE_LIMIT = 64


def slug_of(title: str) -> str:
    """A branch and directory name. Bounded, lowercase, no separators.

    It becomes `fleet/<n>-<slug>` and `<worktree_root>/<n>-<slug>`: a slash would
    make a nested ref, a space would need quoting in every shell call downstream,
    and a 200-character issue title makes a path some filesystems refuse.

    Two different titles can slug to the same string -- `eval/lid: clamp` and
    `eval lid clamp` do -- and that is deliberately not fixed here. The task
    number in front of it is what keeps two tasks apart, so a collision is only
    ever within one task, where it is the same worktree by definition. A hash
    suffix would buy nothing and cost the property `herdr.task_of` depends on:
    that a path names its task.
    """
    slug = _UNSAFE.sub("-", title.lower()).strip("-")[:_SLUG_MAX].strip("-")
    # `or` and not `if not slug.strip()`: the empty string is the only falsy
    # value this expression can produce, and it is exactly the case meant.
    return slug or _SLUG_FALLBACK


def build_prompt(cfg: FleetConfig, gh: GhClient, task: int, role: str) -> str:
    """Role prompt + issue body + latest handoff packet + latest verdict +
    AGENTS.md. Nothing else.

    Parent §7.2 is exact about the list, and parent §6's zero-history test is
    enforced by this function's construction rather than by anyone's discipline.
    Every extra thing added here weakens that test silently -- the packet stops
    having to be sufficient, and nobody finds out until the packet is the only
    thing a receiver gets.

    The verdict is the one addition to §7.2's list, and it is a packet, not
    history. A reviewer leaves REVIEW by writing a verdict, never a handoff, so
    without it the next stage cannot act on the review at all: run
    2026-09-25T11-23-12Z on project-b sent three fix-pass implementers back
    with no findings (each handed the same head straight back) and an integrator
    to escalate asking whether an approved verdict existed.

    A missing role prompt raises. An agent handed an empty role does something
    plausible and wrong, and a blank prompt buries the policy error that caused
    it. A missing AGENTS.md does not: a target repo that has not written one yet
    is a repo without one, not a misconfiguration.
    """
    role_prompt = (cfg.roles_dir / f"{role}.md").read_text(encoding="utf-8")
    issue = gh.get_issue(task)
    agents_md = ""
    candidate = cfg.root / "AGENTS.md"
    if candidate.is_file():
        agents_md = candidate.read_text(encoding="utf-8")
    comments = issue.get("comments") or []
    latest = packet_module.parse_latest(comments, "handoff")
    verdict = packet_module.parse_latest(comments, "verdict")
    parts = [
        role_prompt,
        f"# Issue #{task}: {issue.get('title', '')}\n\n{issue.get('body', '')}",
    ]
    if latest is not None:
        parts.append("# Handoff packet\n\n```json\n" + json.dumps(latest, indent=2) + "\n```")
    if verdict is not None:
        parts.append(
            "# Latest review verdict\n\nBound to the `head_sha` it names; a verdict on an "
            "older head does not approve a newer one.\n\n```json\n"
            + json.dumps(verdict, indent=2)
            + "\n```"
        )
    if agents_md:
        parts.append(agents_md)
    return "\n\n---\n\n".join(parts)


class HerdrExecutor:
    """Implements M2's Executor against real worktrees, agents and GitHub."""

    def __init__(
        self, herdr: Herdr, gh: GhClient, cfg: FleetConfig, owner: str | None = None
    ) -> None:
        self.herdr = herdr
        self.gh = gh
        self.cfg = cfg
        self.owner = owner
        #: Cleanup failures this executor swallowed, newest last and bounded.
        #: A hand-off buffer, not a record: `Daemon.tick` empties it every tick
        #: into the one bounded record the snapshot publishes, so there is
        #: exactly one place an operator reads them and no second copy to
        #: disagree with it. The bound is what makes a herdr that fails every
        #: removal survivable if a tick ever dies before the drain.
        self.failures: deque[str] = deque(maxlen=_FAILURE_LIMIT)
        #: The seeded label set, built on first use; see `_ensure_runtime_labels`.
        self._vocabulary: set[str] | None = None
        #: Runtime labels this process has already created.
        self._ensured: set[str] = set()

    def agents(self) -> list[AgentState]:
        return self.herdr.agents()

    def unpriced(self) -> set[str]:
        """Models the last `agents()` read could not price.

        Delegated rather than cached: the daemon holds an Executor and never a
        Herdr, and this is the fact that turns into a Drain. A cached answer
        would keep the fleet drained through the operator's own fix.
        """
        return self.herdr.unpriced()

    def apply(self, effects: list[Effect]) -> None:
        for effect in effects:
            if isinstance(effect, SetLabels):
                # Add first. expand._to_state excludes the added label from
                # `remove` precisely so this order is safe; reversed, a failure
                # between the two calls strands the task with no state label at
                # all, invisible to every label query.
                self._ensure_runtime_labels(effect.add)
                self.gh.add_labels(effect.task, list(effect.add))
                self.gh.remove_labels(effect.task, list(effect.remove))
            elif isinstance(effect, Assign):
                self.gh.assign(effect.task, effect.owner)
            elif isinstance(effect, Comment):
                self.gh.comment(effect.task, effect.body)
            elif isinstance(effect, CloseIssue):
                self.gh.close_issue(effect.task)
            elif isinstance(effect, StartAgent):
                self._begin(effect)
            elif isinstance(effect, StopAgent):
                self._stop(effect.task)
            else:
                raise ValueError(f"cannot apply {effect!r}")

    def _ensure_runtime_labels(self, names: tuple[str, ...]) -> None:
        """Create each label outside the seeded vocabulary, once per process.

        An owner's answer raises a task's caps past the policy's
        (`model.effective_caps`), so `fleet:attempt-3` on a two-attempt policy is
        a legitimate label the seed never created. gh refuses an unknown label by
        failing the whole edit and changing nothing, so without this the answered
        task's release never lands. The vocabulary is built from the same config
        counters and ownership areas the seed uses, read once; `ensure_label`
        tolerates a label that already exists, so a restart that ensures one
        again is harmless.
        """
        if self._vocabulary is None:
            self._vocabulary = labels_module.static_vocabulary(
                self.cfg.max_attempts,
                self.cfg.max_fix_passes,
                self.cfg.max_rebases,
                policy_module.area_names(self.cfg),
            )
        for name in names:
            if name not in self._vocabulary and name not in self._ensured:
                self.gh.ensure_label(name)
                self._ensured.add(name)

    def _begin(self, effect: StartAgent) -> None:
        """Start the agent. A herdr failure is recorded, not raised.

        Same reason the stop is: the batch is one whole tick's effects, and one
        task that cannot start is not worth every other task's.

        It does not hide the broken task either. The `SetLabels` that precedes
        this in the same batch has already moved it to claimed, so the next
        tick's REAP sees a claimed row with no agent, emits `Release(no_agent)`
        and burns an attempt -- escalating to a human at `max_attempts`. That is
        a self-limiting loop with a counter. Raising here has none: the checkout
        that made the create fail is still there next tick, and the tick dies at
        the same task forever, dropping every later effect in it.

        `HerdrError` only, so a missing role prompt (FileNotFoundError) still
        raises. That is a policy error identical for every task, and burning
        every task's attempt budget against it, silently, is not recovery.
        """
        try:
            self._start(effect)
        except HerdrError as exc:
            self._record(effect.task, "start agent", exc)

    def _start(self, effect: StartAgent) -> None:
        issue = self.gh.get_issue(effect.task)
        self.herdr.create_worktree(effect.task, slug_of(issue.get("title", "")), effect.base)
        # Ownership is written here because this is the layer that knows the
        # identity gh is authenticated as. `fleet-1` is not a GitHub login, and
        # gh fails the whole edit on an assignee it cannot resolve.
        if self.owner:
            self.gh.assign(effect.task, self.owner)
        self.herdr.start_agent(
            effect.task, effect.role, build_prompt(self.cfg, self.gh, effect.task, effect.role)
        )

    def _stop(self, task: int) -> None:
        """Stop the agent, then remove its worktree. Neither may kill the tick.

        The worktree goes with the agent: a re-dispatch creates one at the same
        path on the same branch, and a leftover makes that create fail --
        turning one stall into a task that never runs again.

        Both calls are best-effort, and for the same reason: this is the only
        effect whose desired end state is an *absence*, so the two ways it fails
        are "already absent" and "cannot be made absent", and neither is worth
        the rest of the tick. Concretely --

        * `stop_agent` shells `herdr agent send-keys fleet-<n>`, which answers
          `agent_not_found` and exits 1 when the agent has gone. That is the
          ledger-drift scenario's ordinary path, not an exception: decide emits
          `Release(no_agent)` precisely when the snapshot shows no agent, and
          expand turns it into this effect. Raising here would crash on the
          drift the effect exists to clear -- and skip the worktree removal,
          which is the half that still had work to do.
        * `remove_worktree` clears a checkout that has no herdr workspace by
          falling back to `git worktree remove --force`, and raises only when
          that fails too -- a checkout on a filesystem that is gone, say. At
          that point nothing is left to try: the message names the path and the
          command a human runs, the task will fail its next dispatch on the
          occupied path, and that is one task, where an exception out of
          `apply` is every task in the batch.

        The failure is recorded and logged rather than swallowed. Silence here
        would make a fleet-wide herdr outage look like a quiet night.

        The removal is conditional on the stop, though, and that is the whole
        reason `HerdrError` carries a code. `_subprocess` collapses every
        non-zero exit into one exception, so without it a stop that failed
        because herdr rejected the key syntax -- `invalid_key`, measured live,
        which is why `stop_agent` sends the named `C-c` twice -- reads exactly
        like one that failed because the agent had already gone. Removing the
        worktree on the first of those deletes a LIVE agent's checkout while it
        keeps running and spending, and the orphan rule re-emits `Retire` every
        tick, so it would repeat in silence.
        """
        if not self._stop_agent(task):
            # The agent may still be running. Its checkout is still its own, and
            # the next tick gets to try the stop again.
            return
        self._best_effort(task, "remove worktree", self.herdr.remove_worktree)

    def _stop_agent(self, task: int) -> bool:
        """Stop the agent. True when it is known not to be running afterwards."""
        try:
            self.herdr.stop_agent(task)
        except HerdrError as exc:
            self._record(task, "stop agent", exc)
            # `agent_not_found` is the ledger-drift scenario's ordinary path:
            # decide emits Release(no_agent) precisely when the snapshot shows
            # no agent. The stop failing is then the expected answer, and the
            # worktree it left behind is nobody's.
            return exc.code in AGENT_ABSENT_CODES
        return True

    def _best_effort(self, task: int, what: str, call) -> None:
        try:
            call(task)
        except HerdrError as exc:
            # HerdrError only. A GhError or a TypeError from this same call is a
            # defect in this layer, not a worktree that has already gone, and
            # swallowing those would turn a broken executor into a silent one.
            self._record(task, what, exc)

    def _record(self, task: int, what: str, exc: HerdrError) -> None:
        # The code as well as the message. It is the part herdr means as an
        # interface -- the message is prose it may reword -- and it is what
        # tells an operator reading the runlog at 2am whether the fleet hit one
        # gone agent or a herdr whose key syntax changed under it.
        code = f" (code {exc.code})" if exc.code else ""
        message = f"task {task}: could not {what}{code}: {exc}"
        _LOG.warning("%s", message)
        self.failures.append(message)
