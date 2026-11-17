from __future__ import annotations

import re
from pathlib import Path

from fleet import escalation, packet
from fleet.escalation import MARKER_PREFIX, REASON_TEXT, latest, render
from fleet.expand import expand
from fleet.model import Assign, Comment, Escalate, SetLabels, StopAgent
from test_decide_reap import POLICY

DECIDE = Path(escalation.__file__).with_name("decide.py")


def _human_packet() -> str:
    return packet.render(
        packet.build_handoff(
            task=5,
            to="human",
            role="implementer",
            agent="a1",
            run="r1",
            base_sha="a" * 40,
            head_sha="b" * 40,
            branch="b",
            did="tried",
            checks="red",
            commands=["make check"],
            question="Which API should it use?",
            considered="v1 or v2",
        )
    )


def test_render_starts_with_marker():
    body = render("stalled")
    assert body.split("\n", 1)[0] == "<!-- fleet:escalation reason=stalled -->"
    assert MARKER_PREFIX == "<!-- fleet:escalation reason="
    assert REASON_TEXT["stalled"] in body


def test_every_decide_reason_has_text():
    found = re.findall(r'Escalate\([^)]*?"([a-z_]+)"', DECIDE.read_text())
    assert found
    assert set(found) == set(REASON_TEXT)


def test_latest_prefers_newest():
    pkt = _human_packet()
    got = latest([render("stalled"), pkt])
    assert got is not None and got["index"] == 1
    assert got["text"].startswith("Which API should it use?")
    assert "v1 or v2" in got["text"]
    got = latest([pkt, render("stalled")])
    assert got is not None and got["reason"] == "stalled" and got["index"] == 1


def test_latest_none_without_escalation():
    assert latest([]) is None
    assert latest(["looks good", "<!-- fleet.handoff.v1 -->\nnot json"]) is None


def test_expand_escalate_writes_comment_before_state():
    effects = expand(Escalate(task=3, reason="stalled"), POLICY)
    assert [type(e) for e in effects] == [StopAgent, Assign, Comment, SetLabels]
    assert effects[2].body.startswith("<!-- fleet:escalation reason=stalled -->")


def test_escalate_detail_in_comment():
    effects = expand(Escalate(3, "over_budget", "spent 41.20 of 40.00"), POLICY)
    body = next(e for e in effects if isinstance(e, Comment)).body
    assert REASON_TEXT["over_budget"] in body
    assert "spent 41.20 of 40.00" in body


def _handoff_to(state: str) -> str:
    return packet.render(
        packet.build_handoff(
            task=5,
            to=state,
            role="implementer",
            agent="a1",
            run="r1",
            base_sha="a" * 40,
            head_sha="b" * 40,
            branch="b",
            did="did",
            checks="ok",
            commands=["make check"],
        )
    )


def test_latest_ignores_non_object_packet():
    assert latest(["<!-- fleet.handoff.v1 -->\n```json\n[1]\n```"]) is None


def test_latest_ignores_packet_to_other_state():
    assert latest([_handoff_to("review")]) is None


def test_latest_ignores_marker_mentioned_mid_body():
    body = f"see the earlier note\n{render('stalled')}\nthanks"
    assert latest([body]) is None


def test_latest_quoted_marker_inside_packet_is_the_packet():
    body = _human_packet() + "\n" + render("stalled")
    got = latest([body])
    assert got is not None and got["reason"] == "handoff"
    assert got["text"].startswith("Which API should it use?")


def test_latest_crlf_marker():
    got = latest([render("stalled").replace("\n", "\r\n")])
    assert got is not None and got["reason"] == "stalled"
