"""Locate and parse the target repo's fleet.toml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fleet.cost import PRICE_KEYS
from fleet.labels import DEFAULT_MAX_ATTEMPTS, DEFAULT_MAX_FIX_PASSES, DEFAULT_MAX_REBASES

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
    max_rebases: int
    per_task_usd: float
    fleet_usd: float
    prices: dict[str, dict[str, float]]
    socket_path: Path
    runlog_path: Path
    worktree_root: Path
    poll_seconds: int


def _prices(raw: dict) -> dict[str, dict[str, float]]:
    """Dollars per million tokens, by model.

    Every table must carry all four counters. Three of four is the same silent
    hole as an unpriced model: the missing rate prices those tokens at nothing,
    and cache reads are most of what an agent spends.

    An absent [prices] table is an empty mapping, not an error *here*. `fleet
    plan` starts no agents and needs no prices, and requiring the table would
    refuse every fleet.toml written before it existed, on a key only the daemon
    reads.

    It is not permission to run unpriced. An empty mapping prices nothing, so
    `cost.price_transcript` raises `UnpricedModel` for the first agent whose
    transcript names any model at all, `RealHerdr.agents` records that in
    `unpriced()`, and `Daemon.tick` turns a non-empty `unpriced()` into a
    Drain. The refusal is the daemon's, at the moment it would otherwise spend,
    and it is loud there rather than here.
    """
    out: dict[str, dict[str, float]] = {}
    for model, table in raw.items():
        missing = [key for key in PRICE_KEYS if key not in table]
        if missing:
            raise ConfigError(
                f"[prices.{model!r}] is missing {', '.join(missing)}. "
                "A rate left out prices those tokens at zero, which is the hole "
                "the ceiling exists to close."
            )
        out[model] = {key: float(table[key]) for key in PRICE_KEYS}
    return out


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
    daemon = data.get("daemon", {})
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
        max_rebases=int(fleet.get("max_rebases", DEFAULT_MAX_REBASES)),
        per_task_usd=_positive(
            float(budget.get("per_task_usd", DEFAULT_PER_TASK_USD)), "per_task_usd"
        ),
        fleet_usd=_positive(float(budget.get("fleet_usd", DEFAULT_FLEET_USD)), "fleet_usd"),
        prices=_prices(data.get("prices", {})),
        socket_path=Path(daemon.get("socket", root / ".fleet" / "fleet.sock")).expanduser(),
        runlog_path=Path(daemon.get("runlog", root / ".fleet" / "run.jsonl")).expanduser(),
        worktree_root=Path(daemon.get("worktrees", "~/.fleet/worktrees")).expanduser(),
        # Parent §8 writes 60. A tick fetches comments for every live task, so
        # this is more gh calls than M2's single list and secondary rate limits
        # are real -- instrument before lowering it.
        poll_seconds=int(daemon.get("poll_seconds", 60)),
    )
