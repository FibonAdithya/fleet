"""The write path. Every write validates first; a rejected payload writes nothing."""

from __future__ import annotations

from fleet import labels
from fleet.config import FleetConfig
from fleet.gh import GhClient
from fleet.packet import build_handoff, render
from fleet.schemas import validate


def _post(cfg: FleetConfig, gh: GhClient, packet: dict, state: str) -> dict:
    validate(cfg, "handoff", packet)
    issue = packet["task"]
    gh.comment(issue, render(packet))
    gh.remove_labels(issue, labels.all_state_labels())
    gh.add_labels(issue, [labels.state_label(state)])
    return packet


def do_handoff(cfg: FleetConfig, gh: GhClient, *, task: int, to: str, **kw) -> dict:
    gh.get_issue(task)
    return _post(cfg, gh, build_handoff(task=task, to=to, **kw), to)


def do_block(cfg: FleetConfig, gh: GhClient, *, task: int, on: int, **kw) -> dict:
    gh.get_issue(task)
    gh.get_issue(on)
    packet = build_handoff(task=task, to="blocked", blocked_on=on, **kw)
    return _post(cfg, gh, packet, "blocked")


def do_escalate(
    cfg: FleetConfig, gh: GhClient, *, task: int, question: str, considered: str, **kw
) -> dict:
    if not question.strip():
        raise ValueError("escalate needs a specific question, not an empty one")
    if not considered.strip():
        raise ValueError("escalate needs the options already considered")
    gh.get_issue(task)
    packet = build_handoff(
        task=task, to="human", question=question.strip(), considered=considered.strip(), **kw
    )
    return _post(cfg, gh, packet, "human")
