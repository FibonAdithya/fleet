"""Locate and parse the target repo's fleet.toml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fleet.labels import DEFAULT_MAX_ATTEMPTS, DEFAULT_MAX_FIX_PASSES

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

DEFAULT_PER_TASK_USD = 2.0
DEFAULT_FLEET_USD = 25.0


class ConfigNotFound(Exception):
    """No fleet.toml was found in this directory or any parent."""


class ConfigError(Exception):
    """fleet.toml was found and parsed, but a value in it cannot be used."""


def _positive(value: float, key: str) -> float:
    """A budget of zero or less is a misconfiguration, not a setting.

    `fleet_usd = 0` used to mean the drain never fires -- an overnight run with
    no fleet-wide ceiling at all -- because decide() guarded the rule with
    `fleet_usd > 0`. `per_task_usd = 0` is the mirror failure: every agent is
    over budget on its first tick, so the whole backlog is released and then
    escalated. A rule whose purpose is a spending bound must not turn a typo
    into unlimited spend, so the value is refused here and the rule in decide()
    has one meaning.
    """
    if value <= 0:
        raise ConfigError(
            f"[budget] {key} must be greater than zero, got {value!r}. "
            "Zero is not 'no limit': it disables the ceiling this key exists to set."
        )
    return value


@dataclass(frozen=True)
class FleetConfig:
    root: Path
    repo_slug: str
    integration_branch: str
    ownership: Path
    task_classes: Path
    roles_dir: Path
    schemas_dir: Path
    slots: int
    stall_minutes: int
    max_attempts: int
    max_fix_passes: int
    per_task_usd: float
    fleet_usd: float


def _find_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "fleet.toml").is_file():
            return candidate
    raise ConfigNotFound(f"no fleet.toml in {start} or any parent")


def load_config(start: Path | None = None) -> FleetConfig:
    root = _find_root(Path(start or Path.cwd()).resolve())
    data = tomllib.loads((root / "fleet.toml").read_text())
    repo, policy, fleet = data["repo"], data["policy"], data.get("fleet", {})
    budget = data.get("budget", {})
    return FleetConfig(
        root=root,
        repo_slug=repo["slug"],
        integration_branch=repo.get("integration_branch", "integration"),
        ownership=root / policy["ownership"],
        task_classes=root / policy["task_classes"],
        roles_dir=root / policy["roles"],
        schemas_dir=root / policy["schemas"],
        slots=int(fleet.get("slots", 6)),
        stall_minutes=int(fleet.get("stall_minutes", 10)),
        max_attempts=int(fleet.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
        max_fix_passes=int(fleet.get("max_fix_passes", DEFAULT_MAX_FIX_PASSES)),
        per_task_usd=_positive(
            float(budget.get("per_task_usd", DEFAULT_PER_TASK_USD)), "per_task_usd"
        ),
        fleet_usd=_positive(float(budget.get("fleet_usd", DEFAULT_FLEET_USD)), "fleet_usd"),
    )
