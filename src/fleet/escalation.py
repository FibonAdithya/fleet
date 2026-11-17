"""The reason a task went to `fleet:human`, written where the owner will read it.

Every Escalate comes with a comment whose first line is a marker, so a reader
that sees `fleet:human` can recover why without parsing the label history.
"""

from __future__ import annotations

from fleet import packet

MARKER_PREFIX = "<!-- fleet:escalation reason="
_MARKER_SUFFIX = " -->"

REASON_TEXT: dict[str, str] = {
    "attempts_exhausted": "The task used all of its attempts without producing an accepted result.",
    "packet_without_target": "An agent handed the task over without naming where it goes next.",
    "packet_target_unknown": "An agent handed the task over to a state fleet does not recognise.",
    "rebases_exhausted": "The task was sent back for re-basing more often than the cap allows.",
    "stage_produced_nothing": "A stage finished cleanly but wrote no handoff packet.",
    "dirty_exit": "The agent exited with uncommitted or unreported work.",
    "stalled": "The agent made no progress for longer than the stall limit.",
    "model_mismatch": "The agent is running on a different model than its role requires.",
    "over_budget": "The agent spent more than the per-task cost ceiling.",
    "unroutable": "No agent kind is configured to take this task's class.",
    "unroutable_stage": "No agent kind is configured to take this task's next stage.",
}


def render(reason: str, detail: str = "") -> str:
    """Marker line, then the plain sentence for `reason`, then `detail` if any."""
    lines = [f"{MARKER_PREFIX}{reason}{_MARKER_SUFFIX}"]
    lines.append(REASON_TEXT.get(reason, f"Escalated: {reason}."))
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def _marker_reason(body: str) -> str | None:
    first = body.split("\n", 1)[0].strip()
    if first.startswith(MARKER_PREFIX) and first.endswith(_MARKER_SUFFIX):
        return first[len(MARKER_PREFIX) : -len(_MARKER_SUFFIX)].strip()
    return None


def _human_packet(body: str) -> dict | None:
    """The comment's handoff packet if it is addressed to a human, by the same
    rules as `packet.parse_latest` (marker, last fenced block, schema match)."""
    found = packet.parse_latest([body], "handoff")
    if found is not None and found.get("to") == "human":
        return found
    return None


def latest(comments: list[str]) -> dict | None:
    """Newest escalation among `comments` as `{"index", "reason", "text"}`, or None.

    An escalation is either a marker comment or a handoff packet to `human`; for
    the packet the text is its question, then what was considered.
    """
    for index in range(len(comments) - 1, -1, -1):
        body = comments[index]
        reason = _marker_reason(body)
        if reason is not None:
            return {"index": index, "reason": reason, "text": body.split("\n", 1)[-1].strip()}
        found = _human_packet(body)
        if found is not None:
            parts = [str(found.get("question", "")), str(found.get("considered", ""))]
            return {"index": index, "reason": "handoff", "text": "\n\n".join(p for p in parts if p)}
    return None
