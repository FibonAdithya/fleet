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
def _read(path: Path, mtime: float) -> dict:
    """`mtime` is part of the cache key, not the body: the daemon is long-lived,
    and a schema edited in the target repo must not stay invisible until restart."""
    return json.loads(path.read_text())


def load_schema(cfg: FleetConfig, name: str) -> dict:
    if name not in SCHEMA_FILES:
        raise SchemaError(f"unknown schema: {name}")
    path = cfg.schemas_dir / SCHEMA_FILES[name]
    if not path.is_file():
        raise SchemaError(f"schema not found: {path}")
    return _read(path, path.stat().st_mtime)


def validate(cfg: FleetConfig, name: str, payload: dict) -> None:
    try:
        jsonschema.validate(payload, load_schema(cfg, name))
    except jsonschema.ValidationError as exc:
        field = ".".join(str(p) for p in exc.absolute_path)
        if exc.validator == "required":
            # jsonschema reports a missing property against the PARENT object, so
            # absolute_path is empty for the commonest failure there is. Name the
            # missing keys explicitly or the message says "<root>".
            missing = [k for k in exc.validator_value if k not in exc.instance]
            if missing:
                field = ".".join(filter(None, [field, ", ".join(missing)]))
        raise SchemaError(f"{name} invalid at {field or '<root>'}: {exc.message}") from exc
