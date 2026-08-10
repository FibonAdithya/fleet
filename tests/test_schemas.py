from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from fleet.config import load_config
from fleet.schemas import SchemaError, load_schema, validate

FIXTURE_SCHEMAS = Path.home() / "TIG/fleet-fixture/.agents/schemas"

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


@pytest.fixture()
def cfg(tmp_path):
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = ".agents/roles"\nschemas = ".agents/schemas"\n'
    )
    shutil.copytree(FIXTURE_SCHEMAS, tmp_path / ".agents" / "schemas")
    return load_config(tmp_path)


def test_load_handoff_schema(cfg):
    assert load_schema(cfg, "handoff")["title"] == "fleet.handoff.v1"


def test_valid_packet_passes(cfg):
    validate(cfg, "handoff", VALID)


def test_missing_required_field_is_rejected(cfg):
    bad = {k: v for k, v in VALID.items() if k != "head_sha"}
    with pytest.raises(SchemaError):
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
