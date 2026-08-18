"""Label vocabulary. The ledger's schema lives here."""

from __future__ import annotations

STATES = ("triage", "ready", "claimed", "review", "integrating", "blocked", "human", "done")
AREAS = ("eval", "train", "models", "data", "configs", "docs")
CLASSES = ("patch", "spec", "investigation", "integration")

AUTO_OK = "fleet:auto-ok"

# The counters the vocabulary is sized from. `fleet.toml` can raise either one,
# so config.py reads its defaults from here rather than repeating the literals:
# a vocabulary that disagrees with policy is a label gh has never heard of.
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_MAX_FIX_PASSES = 1
DEFAULT_MAX_REBASES = 1


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


def fix_pass_label(n: int) -> str:
    if n < 1:
        raise ValueError("fix passes start at 1")
    return f"fleet:fix-pass-{n}"


def rebase_label(n: int) -> str:
    if n < 1:
        raise ValueError("rebases start at 1")
    return f"fleet:rebase-{n}"


def all_state_labels() -> list[str]:
    return [f"fleet:{s}" for s in STATES]


def static_vocabulary(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_fix_passes: int = DEFAULT_MAX_FIX_PASSES,
    max_rebases: int = DEFAULT_MAX_REBASES,
) -> set[str]:
    """Exactly the labels created up front in the target repo. Anything outside
    this set is generated at runtime and must be created before it is applied.

    The counters are arguments, not literals, because `fleet.toml` can raise
    them. Hardcoding `attempt-1`/`attempt-2` made the vocabulary silently
    disagree with policy: `max_attempts = 3` yields a well-formed
    `fleet:attempt-3` from attempt_label that this set has never heard of, so
    FakeGh rejects it in every test and gh rejects it live -- nothing creates it.
    Seeding a repo from this same function is what keeps the two in step.
    """
    return {
        *all_state_labels(),
        AUTO_OK,
        *(f"area:{a}" for a in AREAS),
        *(f"class:{c}" for c in CLASSES),
        *(attempt_label(n) for n in range(1, max_attempts + 1)),
        *(fix_pass_label(n) for n in range(1, max_fix_passes + 1)),
        *(rebase_label(n) for n in range(1, max_rebases + 1)),
    }
