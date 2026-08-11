from __future__ import annotations

import json

import pytest

from fleet.schemas import SchemaError, load_schema, validate

VALID = {
    "schema": "fleet.handoff.v1",
    "task": 21,
    "to": "review",
    "from": {"role": "implementer", "agent": "fleet-3", "run": "2026-08-10T02:14:00Z"},
    "base_sha": "fb5ec5d",
    "head_sha": "a91c204",
    "branch": "fleet/21-lid-clamp",
    "did": "Clamped neighbour_count to at least 1; added tests/eval/test_lid.py::test_small_n",
    "evidence": {"checks": "make check: pass", "commands": ["make check"], "artifacts": []},
    "open_questions": [],
    "do_not": ["Do not change the gate band."],
    "discovered": [],
}


def test_load_handoff_schema(cfg):
    assert load_schema(cfg, "handoff")["title"] == "fleet.handoff.v1"


def test_valid_packet_passes(cfg):
    validate(cfg, "handoff", VALID)


def test_missing_required_field_is_rejected(cfg):
    bad = {k: v for k, v in VALID.items() if k != "head_sha"}
    with pytest.raises(SchemaError):
        validate(cfg, "handoff", bad)


def test_missing_required_field_message_names_the_field(cfg):
    """jsonschema reports `required` failures against the parent object, so the
    naive absolute_path read says "<root>" for the commonest failure of all."""
    bad = {k: v for k, v in VALID.items() if k != "head_sha"}
    with pytest.raises(SchemaError, match="head_sha"):
        validate(cfg, "handoff", bad)


def test_unknown_field_is_rejected(cfg):
    with pytest.raises(SchemaError):
        validate(cfg, "handoff", {**VALID, "vibes": "good"})


def test_non_sha_head_is_rejected(cfg):
    with pytest.raises(SchemaError):
        validate(cfg, "handoff", {**VALID, "head_sha": "HEAD"})


def test_empty_evidence_commands_is_rejected(cfg):
    bad = {**VALID, "evidence": {"checks": "pass", "commands": []}}
    with pytest.raises(SchemaError):
        validate(cfg, "handoff", bad)


def test_unknown_role_is_rejected(cfg):
    bad = {**VALID, "from": {**VALID["from"], "role": "vibes-officer"}}
    with pytest.raises(SchemaError):
        validate(cfg, "handoff", bad)


def test_verdict_requires_a_head_sha(cfg):
    verdict = {
        "schema": "fleet.verdict.v1",
        "task": 21,
        "verdict": "approved",
        "reviewer": "fleet-4",
        "findings": [],
    }
    with pytest.raises(SchemaError):
        validate(cfg, "verdict", verdict)
    validate(cfg, "verdict", {**verdict, "head_sha": "a91c204"})


def test_schema_error_names_the_offending_field(cfg):
    with pytest.raises(SchemaError, match="head_sha"):
        validate(cfg, "handoff", {**VALID, "head_sha": "zzz"})


def test_round_trip_through_json(cfg):
    validate(cfg, "handoff", json.loads(json.dumps(VALID)))


def test_a_malformed_run_timestamp_is_rejected(cfg):
    """`from.run` orders the permanent audit trail, and it was the one field in
    the schema with no enforced constraint: jsonschema treats `format` as an
    annotation unless a FormatChecker is supplied, and the date-time check
    itself lives in rfc3339-validator. This fails if either goes missing."""
    bad = {**VALID, "from": {**VALID["from"], "run": "tuesday-ish"}}
    with pytest.raises(SchemaError, match="run"):
        validate(cfg, "handoff", bad)


def test_a_well_formed_run_timestamp_still_passes(cfg):
    validate(cfg, "handoff", {**VALID, "from": {**VALID["from"], "run": "2026-08-10T02:14:00Z"}})


@pytest.mark.parametrize(
    "payload",
    [
        {"to": "review", "blocked_on": 2},
        {"to": "done", "blocked_on": 2},
        {"to": "review", "question": "why?"},
        {"to": "human", "question": "q", "considered": "c", "blocked_on": 2},
        {"to": "blocked"},
        {"to": "human"},
        {"to": "human", "question": "q"},
    ],
    ids=lambda p: f"{p['to']}+{'+'.join(k for k in p if k != 'to') or 'nothing'}",
)
def test_the_conditional_fields_are_bound_to_their_state_by_the_schema(cfg, payload):
    """build_handoff enforces this too, but only for callers that go through it.
    The schema is what holds for a packet recovered from a comment, or written
    by anything that is not this CLI."""
    with pytest.raises(SchemaError):
        validate(cfg, "handoff", {**VALID, **payload})


@pytest.mark.parametrize(
    "payload",
    [
        {"to": "blocked", "blocked_on": 2},
        {"to": "human", "question": "which band?", "considered": "0.42-0.58 vs 0.62"},
        {"to": "done"},
        {"to": "integrating"},
    ],
    ids=lambda p: p["to"],
)
def test_a_correctly_shaped_packet_still_passes(cfg, payload):
    validate(cfg, "handoff", {**VALID, **payload})


def test_a_schema_rejection_still_names_the_offending_field(cfg):
    """`{"not": {}}` under `properties` rather than a boolean `false` or a
    top-level not/required pair: jsonschema drops the instance path for a
    boolean schema, and the top-level form reports against the whole packet."""
    with pytest.raises(SchemaError, match="at blocked_on"):
        validate(cfg, "handoff", {**VALID, "to": "review", "blocked_on": 2})


def test_a_malformed_schema_file_raises_ours_not_jsonschemas(cfg):
    """A structurally invalid schema in the target repo makes jsonschema raise
    SchemaError, which is not a ValidationError -- so it used to sail past the
    handler and out of the CLI as a bare third-party traceback, defeating this
    module's "the message names the offending field" contract."""
    (cfg.schemas_dir / "handoff.schema.json").write_text('{"type": 17}')
    with pytest.raises(SchemaError, match="malformed schema"):
        validate(cfg, "handoff", VALID)
