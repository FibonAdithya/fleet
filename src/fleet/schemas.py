"""Schema loading and validation. A packet that does not validate never lands."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jsonschema
from jsonschema.exceptions import SchemaError as JsonSchemaError
from jsonschema.exceptions import best_match

from fleet.config import FleetConfig

SCHEMA_FILES = {"handoff": "handoff.schema.json", "verdict": "verdict.schema.json"}

# `format` is an annotation, not a constraint, unless a checker is supplied --
# without this, handoff.schema.json's `"format": "date-time"` on `from.run` is
# decorative and `run: "tuesday-ish"` lands in the permanent audit trail. The
# date-time check itself lives in rfc3339-validator, which is why that package is
# a hard dependency; test_a_malformed_run_timestamp_is_rejected fails loudly if
# it ever goes missing rather than letting the constraint quietly switch off.
_FORMAT_CHECKER = jsonschema.FormatChecker()


class SchemaError(Exception):
    """A payload failed validation. The message names the offending field."""


@lru_cache(maxsize=8)
def _read(path: Path, mtime: float) -> dict:
    """`mtime` is part of the cache key, not the body: the daemon is long-lived,
    and a schema edited in the target repo must not stay invisible until restart."""
    return json.loads(path.read_text())


@lru_cache(maxsize=8)
def _validator(path: Path, mtime: float) -> jsonschema.Draft202012Validator:
    """A validator per (path, mtime), for two reasons.

    `jsonschema.validate` re-checks the schema against its metaschema on every
    single call -- far more expensive than the file read `_read` already caches,
    and this is the write path of a long-lived daemon.

    It also raises `jsonschema.SchemaError` for a structurally invalid schema
    file, which is not a subclass of ValidationError and so would sail past
    `validate`'s handler and out of the CLI as a bare third-party traceback.
    Doing the check once, here, converts it to ours.
    """
    schema = _read(path, mtime)
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except JsonSchemaError as exc:
        raise SchemaError(f"malformed schema {path}: {exc.message}") from exc
    return jsonschema.Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)


def _schema_path(cfg: FleetConfig, name: str) -> Path:
    if name not in SCHEMA_FILES:
        raise SchemaError(f"unknown schema: {name}")
    path = cfg.schemas_dir / SCHEMA_FILES[name]
    if not path.is_file():
        raise SchemaError(f"schema not found: {path}")
    return path


def load_schema(cfg: FleetConfig, name: str) -> dict:
    path = _schema_path(cfg, name)
    return _read(path, path.stat().st_mtime)


def validate(cfg: FleetConfig, name: str, payload: dict) -> None:
    path = _schema_path(cfg, name)
    validator = _validator(path, path.stat().st_mtime)
    # best_match, not the first error iter_errors yields: it is what
    # jsonschema.validate uses to pick the most relevant failure out of a nested
    # anyOf/oneOf, and the message is what the operator has to act on.
    exc = best_match(validator.iter_errors(payload))
    if exc is None:
        return
    field = ".".join(str(p) for p in exc.absolute_path)
    if exc.validator == "required":
        # jsonschema reports a missing property against the PARENT object, so
        # absolute_path is empty for the commonest failure there is. Name the
        # missing keys explicitly or the message says "<root>".
        missing = [k for k in exc.validator_value if k not in exc.instance]
        if missing:
            field = ".".join(filter(None, [field, ", ".join(missing)]))
    raise SchemaError(f"{name} invalid at {field or '<root>'}: {exc.message}") from exc
