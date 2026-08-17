"""Replay a whole night through decide(), offline and deterministically.

No clock and no I/O beyond reading the scenario and its seeds, so a stall that
takes eleven simulated minutes costs no wall-clock at all.

The schema is strict in both directions. An unknown key raises, and so does a
missing one: a scenario is a proof, and the failure mode of a lenient scenario
parser is a file that reports a pass having asserted nothing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from fleet import ledger as ledger_module
from fleet import policy as policy_module
from fleet.config import load_config
from fleet.decide import decide
from fleet.executor import FakeExecutor
from fleet.expand import expand
from fleet.model import (
    Advance,
    Dispatch,
    Drain,
    Effect,
    Escalate,
    FleetState,
    Intent,
    Release,
    SetLabels,
    Unblock,
)
from fleet.runlog import RunLog

START = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)

_KEYS = {"name", "proves", "seed", "ticks"}
_SEED_KEYS = {"issues", "ledger", "packets", "policy"}
_TICK_KEYS = {"expect", "outcomes", "packets"}

# Parsed structurally, not string-compared: pyyaml splits an unquoted
# `[Dispatch(1, implementer, 1)]` into three scalars, and a string comparison
# turns that into a silent mismatch instead of a complaint about the file.
_CALL = re.compile(r"^(\w+)\((.*)\)$")
_INTENTS = (Advance, Dispatch, Drain, Escalate, Release, Unblock)
_INTENT_NAMES = frozenset(cls.__name__ for cls in _INTENTS)


class ScenarioError(Exception):
    """A scenario file could not be understood."""


def load_scenario(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def render_intent(intent: Intent) -> str:
    """`Dispatch(5, implementer, 2)` — the form scenarios are written in.

    Every field in declaration order except `base`, which is `policy
    .integration_branch` for every dispatch M2 makes and so carries no
    information a scenario could assert on. The attempt number is rendered
    precisely because it does: it is the "attempt += 1" half of the stall and
    poison-task claims, and a renderer that dropped it would let
    `Release(1, stalled)` match a release on any attempt, the third included.
    """
    values = [str(v) for k, v in vars(intent).items() if k != "base"]
    return f"{type(intent).__name__}({', '.join(values)})"


def _shape(text: object) -> tuple[str, tuple[str, ...]]:
    """`"Dispatch(5, implementer, 2)"` -> `("Dispatch", ("5", "implementer", "2"))`."""
    if not isinstance(text, str):
        raise ScenarioError(f"cannot parse expectation {text!r}: expected a quoted string")
    match = _CALL.match(text.strip())
    if match is None:
        raise ScenarioError(
            f"cannot parse expectation {text!r}: expected Name(arg, ...), quoted. "
            "An unquoted YAML flow entry splits on every comma."
        )
    name, inside = match.group(1), match.group(2).strip()
    if name not in _INTENT_NAMES:
        raise ScenarioError(f"{name!r} is not an intent; expected one of {sorted(_INTENT_NAMES)}")
    args = tuple(part.strip() for part in inside.split(",")) if inside else ()
    return name, args


def _check(scenario: dict) -> None:
    unknown = set(scenario) - _KEYS
    if unknown:
        raise ScenarioError(f"unknown key(s) in scenario: {sorted(unknown)}")
    for key in ("name", "proves"):
        if not str(scenario.get(key) or "").strip():
            raise ScenarioError(f"scenario has no {key}")
    if not scenario.get("seed"):
        raise ScenarioError("scenario has no seed")
    if not scenario.get("ticks"):
        raise ScenarioError("scenario has no ticks")

    seed = scenario["seed"]
    unknown_seed = set(seed) - _SEED_KEYS
    if unknown_seed:
        raise ScenarioError(f"unknown key(s) in seed: {sorted(unknown_seed)}")
    if not seed.get("policy"):
        raise ScenarioError("seed has no policy")
    if "ledger" in seed and "issues" in seed:
        # One of the two would win silently, so half the file would be a
        # description of a ledger the run never read.
        raise ScenarioError("seed names both a ledger and inline issues; pick one")

    for index, tick in enumerate(scenario["ticks"], start=1):
        unknown_tick = set(tick) - _TICK_KEYS
        if unknown_tick:
            raise ScenarioError(f"unknown key(s) in tick {index}: {sorted(unknown_tick)}")
        if tick.get("expect") is None:
            # Absent meant "do not check", so one mistyped key produced a green
            # scenario proving nothing. `expect: []` is the way to assert an
            # absence; there is no way to assert nothing at all.
            raise ScenarioError(
                f"tick {index} has no expect; use `expect: []` to assert an absence"
            )


def _label_names(raw: list) -> list[str]:
    """gh returns label objects; a hand-written seed writes names. Normalize
    once, here, so the row fold below never has to know which shape it holds --
    and so simulate does not reach into ledger's private helpers to find out."""
    names = []
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else item
        if not name:
            raise ScenarioError(f"seeded row carries a label with no name: {item!r}")
        names.append(name)
    return names


def _rows(seed: dict, root: Path) -> list[dict]:
    if "ledger" in seed:
        raw = json.loads((root / seed["ledger"]).read_text(encoding="utf-8"))
    else:
        raw = list(seed.get("issues", []))
    return [dict(row, labels=_label_names(row.get("labels", []))) for row in raw]


def _numbered(mapping: dict | None) -> dict[int, dict]:
    """YAML gives `{5: ...}` an int key and `{"5": ...}` a str one."""
    return {int(key): value for key, value in (mapping or {}).items()}


def _apply_to_rows(rows: list[dict], effects: list[Effect]) -> list[dict]:
    """Fold this intent's label effects back into the seeded rows, so the next
    tick reads a ledger that reflects the last one.

    Only `SetLabels` is folded. `decide()` reads no other field a scenario can
    observe -- notably not `assignee` -- and a fold no assertion can see is code
    no test can keep honest.
    """
    updated = [dict(row, labels=list(row["labels"])) for row in rows]
    by_number = {row["number"]: row for row in updated}
    for effect in effects:
        if not isinstance(effect, SetLabels):
            continue
        target = by_number.get(effect.task)
        if target is None:
            continue
        kept = [name for name in target["labels"] if name not in effect.remove]
        target["labels"] = kept + [name for name in effect.add if name not in kept]
    return updated


def run(scenario: dict, root: Path, runlog: RunLog | None = None) -> list[list[Intent]]:
    _check(scenario)
    seed = scenario["seed"]
    # Joined to root exactly as `ledger` is. Unjoined, load_config resolves
    # against the CWD and _find_root walks UP from there, so a relative miss is
    # not an error -- it is some other repo's fleet.toml, silently.
    cfg = load_config(root / seed["policy"])
    policy = policy_module.load(cfg)
    rows = _rows(seed, root)
    seed_packets = _numbered(seed.get("packets"))

    executor = FakeExecutor(per_task_usd=policy.per_task_usd)
    state = FleetState()
    now = START
    history: list[list[Intent]] = []

    for index, tick in enumerate(scenario["ticks"], start=1):
        handovers = _numbered(tick.get("packets"))
        for task, outcome in _numbered(tick.get("outcomes")).items():
            try:
                executor.script(task, outcome, packet=handovers.get(task))
            except ValueError as exc:
                raise ScenarioError(f"tick {index}: {exc}") from exc
        executor.tick()

        packets = {**seed_packets, **executor.packets()}
        tasks = ledger_module.read(rows, packets=packets)
        intents, state = decide(state, tasks, policy, executor.agents(), now)
        history.append(intents)

        for intent in intents:
            # Expanded once and reused. Two expansions with different attempt
            # numbers made FakeExecutor.applied record a label sequence that
            # never happened -- for exactly the counter the claims are about.
            effects = expand(intent, policy)
            executor.apply(effects)
            rows = _apply_to_rows(rows, effects)
            if runlog is not None:
                runlog.append(tick=index, at=now, intent=intent, outcome="applied")

        actual = sorted(_shape(render_intent(i)) for i in intents)
        expected = sorted(_shape(entry) for entry in tick["expect"])
        if actual != expected:
            raise AssertionError(
                f"tick {index} of {scenario['name']}: expected {sorted(tick['expect'])}, "
                f"got {sorted(render_intent(i) for i in intents)}"
            )
        now += timedelta(minutes=policy.stall_minutes)

    return history
