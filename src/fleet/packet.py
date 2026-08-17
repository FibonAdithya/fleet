"""Build, render and recover handoff packets from issue comments."""

from __future__ import annotations

import json
import re

MARKERS = {"handoff": "<!-- fleet.handoff.v1 -->", "verdict": "<!-- fleet.verdict.v1 -->"}
SCHEMA_IDS = {"handoff": "fleet.handoff.v1", "verdict": "fleet.verdict.v1"}

_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)

# Which of the three conditional fields each terminal state requires. Every other
# state requires none of them, and no state tolerates the ones it does not
# require -- see _check_conditional_fields.
_REQUIRED_BY_STATE = {"blocked": ("blocked_on",), "human": ("question", "considered")}
_CONDITIONAL_FIELDS = ("blocked_on", "question", "considered")


def _check_conditional_fields(task: int, to: str, present: dict) -> None:
    """Bind `blocked_on`/`question`/`considered` to the state that needs them.

    The schema lists all three as free optional properties, so it accepts a
    REVIEW packet carrying `blocked_on: 7` (a phantom dependency an unblock
    watcher will act on) and a BLOCKED packet carrying none (a task nothing
    could ever resolve). Enforcing it here rather than at the call sites covers
    every caller, including a future one that reaches build_handoff directly.
    """
    required = _REQUIRED_BY_STATE.get(to, ())
    missing = [f for f in required if present.get(f) is None]
    if missing:
        raise ValueError(f"a handoff to {to!r} requires {', '.join(missing)}")
    extra = [f for f in _CONDITIONAL_FIELDS if f not in required and present.get(f) is not None]
    if extra:
        raise ValueError(f"a handoff to {to!r} must not carry {', '.join(extra)}")
    if present.get("blocked_on") == task:
        # Both issues resolve and the schema is satisfied, so nothing downstream
        # catches this: the auto-unblock watcher waits for the very task it is
        # blocking, no human is woken (block is the non-escalating path), and the
        # task sits until the stall timer. A `--task 4 --on 4` typo is enough.
        raise ValueError(f"task {task} cannot block on itself")


def build_handoff(
    *,
    task: int,
    to: str,
    role: str,
    agent: str,
    run: str,
    base_sha: str,
    head_sha: str,
    branch: str,
    did: str,
    checks: str,
    commands: list[str],
    artifacts: list[str] | None = None,
    open_questions: list[str] | None = None,
    do_not: list[str] | None = None,
    discovered: list[int] | None = None,
    blocked_on: int | None = None,
    question: str | None = None,
    considered: str | None = None,
) -> dict:
    """Assemble a handoff packet. Schema validation happens separately, in the
    CLI; the invariants the schema cannot express are checked here."""
    _check_conditional_fields(
        task, to, {"blocked_on": blocked_on, "question": question, "considered": considered}
    )
    packet: dict = {
        "schema": "fleet.handoff.v1",
        "task": task,
        "to": to,
        "from": {"role": role, "agent": agent, "run": run},
        "base_sha": base_sha,
        "head_sha": head_sha,
        "branch": branch,
        "did": did,
        "evidence": {"checks": checks, "commands": commands, "artifacts": artifacts or []},
        "open_questions": open_questions or [],
        "do_not": do_not or [],
        "discovered": discovered or [],
    }
    if blocked_on is not None:
        packet["blocked_on"] = blocked_on
    if question is not None:
        packet["question"] = question
    if considered is not None:
        packet["considered"] = considered
    return packet


def render(payload: dict) -> str:
    """A marker line plus a fenced JSON block, so humans and machines both read it."""
    kind = "verdict" if payload.get("schema", "").startswith("fleet.verdict") else "handoff"
    return f"{MARKERS[kind]}\n```json\n{json.dumps(payload, indent=2)}\n```"


def parse_latest(comments: list[str], kind: str) -> dict | None:
    """Return the newest payload of `kind`, or None.

    Scans comments newest-first and, within one comment, takes the LAST fenced
    block: an agent that quotes an earlier packet before appending its own would
    otherwise hand the receiver the quoted one. The recovered payload's `schema`
    must match `kind` -- matching on the marker alone lets a comment carrying
    both a handoff and a verdict return the wrong one.
    """
    marker = MARKERS[kind]
    for body in reversed(comments):
        if marker not in body:
            continue
        for block in reversed(_BLOCK.findall(body)):
            try:
                payload = json.loads(block)
            except json.JSONDecodeError:
                continue
            if payload.get("schema") == SCHEMA_IDS[kind]:
                return payload
    return None
