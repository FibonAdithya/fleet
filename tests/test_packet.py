from __future__ import annotations

import json

from fleet.packet import MARKERS, build_handoff, parse_latest, render


def test_build_handoff_sets_schema_and_shape():
    p = build_handoff(
        task=21,
        to="review",
        role="implementer",
        agent="fleet-3",
        run="2026-08-10T02:14:00Z",
        base_sha="fb5ec5d",
        head_sha="a91c204",
        branch="fleet/21-lid",
        did="clamped k",
        checks="make check: pass",
        commands=["make check"],
    )
    assert p["schema"] == "fleet.handoff.v1"
    assert p["from"] == {"role": "implementer", "agent": "fleet-3", "run": "2026-08-10T02:14:00Z"}
    assert p["evidence"]["commands"] == ["make check"]
    assert p["open_questions"] == [] and p["do_not"] == [] and p["discovered"] == []


def test_optional_fields_are_omitted_when_empty():
    p = build_handoff(
        task=21,
        to="review",
        role="implementer",
        agent="a",
        run="2026-08-10T02:14:00Z",
        base_sha="fb5ec5d",
        head_sha="a91c204",
        branch="b",
        did="d",
        checks="c",
        commands=["make check"],
    )
    assert "blocked_on" not in p
    assert "question" not in p


def test_render_contains_marker_and_parses_back():
    p = build_handoff(
        task=21,
        to="review",
        role="implementer",
        agent="a",
        run="2026-08-10T02:14:00Z",
        base_sha="fb5ec5d",
        head_sha="a91c204",
        branch="b",
        did="d",
        checks="c",
        commands=["make check"],
    )
    body = render(p)
    assert MARKERS["handoff"] in body
    assert parse_latest([body], "handoff") == p


def test_parse_latest_returns_the_newest_packet():
    old = render(
        build_handoff(
            task=1,
            to="review",
            role="implementer",
            agent="a",
            run="2026-08-10T01:00:00Z",
            base_sha="aaaaaaa",
            head_sha="bbbbbbb",
            branch="b",
            did="old",
            checks="c",
            commands=["make check"],
        )
    )
    new = render(
        build_handoff(
            task=1,
            to="claimed",
            role="reviewer",
            agent="b",
            run="2026-08-10T02:00:00Z",
            base_sha="aaaaaaa",
            head_sha="ccccccc",
            branch="b",
            did="new",
            checks="c",
            commands=["make check"],
        )
    )
    assert parse_latest([old, "unrelated chatter", new], "handoff")["did"] == "new"


def test_parse_latest_ignores_non_packet_comments():
    assert parse_latest(["just a comment", "```json\n{}\n```"], "handoff") is None


def test_parse_latest_survives_malformed_json_in_a_marked_comment():
    broken = f"{MARKERS['handoff']}\n```json\n{{not json\n```"
    assert parse_latest([broken], "handoff") is None


def _packet(did: str, head: str, to: str = "review", role: str = "implementer") -> str:
    return render(
        build_handoff(
            task=1,
            to=to,
            role=role,
            agent="a",
            run="2026-08-10T01:00:00Z",
            base_sha="aaaaaaa",
            head_sha=head,
            branch="b",
            did=did,
            checks="c",
            commands=["make check"],
        )
    )


def test_parse_latest_takes_the_last_block_when_a_comment_quotes_an_earlier_packet():
    """An agent that quotes the packet it received before appending its own puts
    two blocks in one comment. The receiver must get the new one."""
    quoted = (
        f"Quoting what I received:\n\n{_packet('old', 'bbbbbbb')}\n\n"
        f"Mine:\n\n{_packet('new', 'ccccccc')}"
    )
    assert parse_latest([quoted], "handoff")["did"] == "new"


def test_parse_latest_rejects_a_payload_whose_schema_is_the_wrong_kind():
    verdict = {
        "schema": "fleet.verdict.v1",
        "task": 1,
        "verdict": "approved",
        "head_sha": "a91c204",
        "reviewer": "b",
        "findings": [],
    }
    body = f"{MARKERS['handoff']}\n```json\n{json.dumps(verdict)}\n```"
    assert parse_latest([body], "handoff") is None
