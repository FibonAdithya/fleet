"""Schema loading and validation. A packet that does not validate never lands."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jsonschema

from fleet.config import FleetConfig

SCHEMA_FILES = {"handoff": "handoff.schema.json", "verdict": "verdict.schema.json"}


class SchemaError(Exception):
    """A payload failed validation. The message names the offending field."""


@lru_cache(maxsize=8)
def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def load_schema(cfg: FleetConfig, name: str) -> dict:
    if name not in SCHEMA_FILES:
        raise SchemaError(f"unknown schema: {name}")
    path = cfg.schemas_dir / SCHEMA_FILES[name]
    if not path.is_file():
        raise SchemaError(f"schema not found: {path}")
    return _read(path)


def validate(cfg: FleetConfig, name: str, payload: dict) -> None:
    try:
        jsonschema.validate(payload, load_schema(cfg, name))
    except jsonschema.ValidationError as exc:
        field = ".".join(str(p) for p in exc.absolute_path) or "<root>"
        raise SchemaError(f"{name} invalid at {field}: {exc.message}") from exc
