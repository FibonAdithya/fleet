"""Label vocabulary. The ledger's schema lives here."""

from __future__ import annotations

STATES = ("triage", "ready", "claimed", "review", "integrating", "blocked", "human", "done")
AREAS = ("eval", "train", "models", "data", "configs", "docs")
CLASSES = ("patch", "spec", "investigation", "integration")

AUTO_OK = "fleet:auto-ok"


def state_label(name: str) -> str:
    if name not in STATES:
        raise ValueError(f"unknown state: {name}")
    return f"fleet:{name}"


def area_label(name: str) -> str:
    if name not in AREAS:
        raise ValueError(f"unknown area: {name}")
    return f"area:{name}"


def class_label(name: str) -> str:
    if name not in CLASSES:
        raise ValueError(f"unknown class: {name}")
    return f"class:{name}"


def attempt_label(n: int) -> str:
    if n < 1:
        raise ValueError("attempts start at 1")
    return f"fleet:attempt-{n}"


def all_state_labels() -> list[str]:
    return [f"fleet:{s}" for s in STATES]
