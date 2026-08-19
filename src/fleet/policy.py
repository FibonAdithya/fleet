"""Parse the target repo's policy layer into a Policy.

Strict on purpose. A silently-empty ownership map disables area exclusion, which
means every task looks dispatchable and the fleet runs six agents into one area
-- a failure that passes every test not specifically looking for it. Same class
as M1's fake/real divergences, and the same fix: make the permissive path
impossible rather than remembering not to take it.
"""

from __future__ import annotations

import re
from pathlib import Path

from fleet import labels as labels_module
from fleet.config import FleetConfig
from fleet.cost import PROVIDER_KINDS
from fleet.model import AgentSpec, Policy

_CELL = re.compile(r"^\s*\|(.+)\|\s*$")
_TICKS = re.compile(r"^`(.*)`$")


class PolicyError(Exception):
    """The policy layer could not be understood."""


def _rows(text: str, header: tuple[str, ...]) -> list[list[str]]:
    """Every row of the first markdown table whose header starts with `header`.

    Matching on the header rather than taking the first table is what keeps
    ownership.md's second table -- Non-dispatchable paths, headed Path | Why --
    from being read as glob-to-area rules.
    """
    rows: list[list[str]] = []
    in_table = False
    for line in text.splitlines():
        match = _CELL.match(line)
        if not match:
            in_table = False
            continue
        cells = [c.strip() for c in match.group(1).split("|")]
        lowered = tuple(c.lower() for c in cells[: len(header)])
        if lowered == header:
            in_table = True
            rows = []
            continue
        if not in_table:
            continue
        if all(set(c) <= {"-", ":"} for c in cells if c):
            continue  # the |---|---| separator
        rows.append(cells)
    return rows


def _unticked(cell: str) -> str:
    match = _TICKS.match(cell.strip())
    return match.group(1) if match else cell.strip()


def known_roles(roles_dir: Path) -> frozenset[str]:
    """The role vocabulary: one `.md` prompt file per role, by stem.

    Raises on an empty or absent directory rather than returning an empty set,
    because an empty set makes the route check below vacuous -- every route would
    then validate, which is the permissive path this module exists to close.
    """
    names = frozenset(path.stem for path in roles_dir.glob("*.md"))
    if not names:
        raise PolicyError(f"no role prompts in {roles_dir}; every route would validate")
    return names


def parse_ownership(text: str) -> tuple[tuple[str, str], ...]:
    rows = _rows(text, ("glob", "area"))
    pairs = []
    for cells in rows:
        if len(cells) < 2:
            raise PolicyError(f"malformed ownership row: {cells!r}")
        glob, area = _unticked(cells[0]), _unticked(cells[1])
        if area not in labels_module.AREAS:
            raise PolicyError(f"unknown area {area!r} in ownership map")
        pairs.append((glob, area))
    if not pairs:
        raise PolicyError("no ownership rules found; area exclusion would be disabled")
    return tuple(pairs)


def _role(segment: str, klass: str, roles: frozenset[str]) -> str:
    """The role a route segment names, dropping any prose that follows it.

    task-classes.md's `investigation` route cell reads
    ``implementer, forbidden from writing `src/` ``. The leading token is the
    role; the clause after the comma is a constraint on that role's prompt, not
    an allocator input, and the allocator has nowhere to put it. Dropping it here
    is deliberate -- see Task 17's doc update, which records it as a known gap
    rather than leaving the reader to infer it from this function.

    What is NOT dropped is the check: the leading token must name a real role, or
    the route raises. Without it the whole cell became the "role", `_dispatch`
    handed that string to `StartAgent`, and the fleet asked herdr to start an
    agent under a role prompt that does not exist -- and captured issue #10 is
    `class:investigation`, so any scenario seeded from the real ledger does it.
    """
    role = _unticked(segment.split(",", 1)[0])
    if role not in roles:
        raise PolicyError(f"class {klass!r} routes to unknown role {role!r}")
    return role


def parse_agents(text: str, roles: frozenset[str]) -> tuple[tuple[str, str, str], ...]:
    """The `Role | Agent | Model` table: which CLI and model each role runs on.

    Required, not defaulted. A Policy built without it would run every role on
    whatever CLI and model the operator's shell happened to default to, which is
    the same class of mistake `integration_branch` has no default for.

    `_rows` matches `cells[:len(header)]`, so the three-column header needs no
    parser change, and ownership.md already carries two tables this way.
    """
    rows = _rows(text, ("role", "agent", "model"))
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for cells in rows:
        if len(cells) < 3:
            raise PolicyError(f"malformed role/agent row: {cells!r}")
        role, agent, model = (_unticked(c) for c in cells[:3])
        if role not in roles:
            raise PolicyError(f"unknown role {role!r} in the role/agent table")
        if not agent or not model:
            raise PolicyError(f"role {role!r} has no agent or no model")
        if role in seen:
            raise PolicyError(
                f"role {role!r} is listed twice in the role/agent table; "
                "last-wins would silently discard the first assignment"
            )
        seen.add(role)
        out.append((role, agent, model))
    if not out:
        raise PolicyError(
            "no role/agent table found; every role would fall back to the "
            "operator's default CLI and model, unrecorded"
        )
    return tuple(out)


def parse_routes(text: str, roles: frozenset[str]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    rows = _rows(text, ("class", "route"))
    routes = []
    for cells in rows:
        if len(cells) < 2:
            raise PolicyError(f"malformed task-class row: {cells!r}")
        name = _unticked(cells[0])
        if name not in labels_module.CLASSES:
            raise PolicyError(f"unknown class {name!r} in task classes")
        stages = tuple(
            _role(segment, name, roles) for segment in cells[1].split("→") if segment.strip()
        )
        if not stages:
            raise PolicyError(f"class {name!r} has no route")
        routes.append((name, stages))
    if not routes:
        raise PolicyError("no task classes found; nothing could ever be routed")
    return tuple(routes)


def _validate_agents(
    agents: tuple[tuple[str, str, str], ...],
    agent_specs: dict[str, AgentSpec],
    prices: dict[str, dict[str, float]],
    routes: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    """Every declared agent has a reader, a launch config, and a rate; every
    routed role has a declaration at all.

    The price check is the one that earns its place first. Without a declared
    model an unpriced model is found only when a dispatched agent's transcript
    is read -- mid-run, after spending, which is docs/m3-inputs.md §1.2's
    failure exactly. Declaring it moves that to a startup error, before
    anything is dispatched.

    The launch-config check exists because a declared kind with no
    `[agents.<kind>]` section used to pass this function anyway.
    `HerdrExecutor._start` raises `HerdrError` for it, `_begin` swallows that
    (deliberately -- see its own docstring on why a herdr failure must not take
    the whole tick down), the claimed row ends the tick with no agent, and the
    next tick's REAP sees a claimed task with no agent, emits
    `Release(no_agent)`, and burns an attempt. That is identical for every task
    routed to the role, so the whole backlog walks to NEEDS_HUMAN overnight
    with the fleet never having launched a single agent.

    The routes-coverage check exists because a role that appears in a route but
    has no row in the `Role | Agent | Model` table used to pass this function
    too. It fails the same way at dispatch -- but it is also worse than a
    missing launch config: `Policy.agent_for(role)` returns `None` for it, and
    `_model_mismatch` treats `None` as "not yet known" and returns `False`, so
    the declared-vs-observed model check this branch exists to add is silently
    disabled for that role, with nothing said.
    """
    known = set(PROVIDER_KINDS)
    for role, kind, model in agents:
        if kind not in known:
            raise PolicyError(
                f"role {role!r} is routed to agent {kind!r}, which has no transcript "
                f"reader. Known: {sorted(known)}. Its agents could not be priced and "
                "the fleet would drain on the first tick."
            )
        if kind not in agent_specs:
            raise PolicyError(
                f"role {role!r} is routed to agent kind {kind!r}, but fleet.toml has "
                f"no [agents.{kind}] launch config for it. Startup would pass and "
                "then every dispatch of this role would fail identically at "
                "HerdrExecutor._start: the failure is swallowed per task, the claimed "
                "row ends the tick with no agent, the next tick releases it and burns "
                "an attempt, and the whole backlog walks to NEEDS_HUMAN overnight "
                "having launched nothing."
            )
        if model not in prices:
            raise PolicyError(
                f"role {role!r} declares model {model!r}, which has no [prices] row "
                "in fleet.toml. Dispatching it would drain the run mid-night rather "
                "than failing here, before anything is spent."
            )

    declared_roles = {role for role, _, _ in agents}
    for klass, stages in routes:
        for role in stages:
            if role not in declared_roles:
                raise PolicyError(
                    f"class {klass!r} routes to role {role!r}, which has no row in "
                    "the Role | Agent | Model table. Dispatch would fail for it the "
                    "same way a missing [agents.<kind>] does -- and worse: "
                    "Policy.agent_for(role) returns None for it, so the "
                    "declared-vs-observed model mismatch check is silently disabled "
                    "for this role as well."
                )


def load(cfg: FleetConfig) -> Policy:
    roles = known_roles(cfg.roles_dir)
    classes_text = cfg.task_classes.read_text()
    agents = parse_agents(classes_text, roles)
    routes = parse_routes(classes_text, roles)
    _validate_agents(agents, cfg.agents, cfg.prices, routes)
    return Policy(
        areas=parse_ownership(cfg.ownership.read_text()),
        routes=routes,
        agents=agents,
        slots=cfg.slots,
        stall_minutes=cfg.stall_minutes,
        max_attempts=cfg.max_attempts,
        max_fix_passes=cfg.max_fix_passes,
        max_rebases=cfg.max_rebases,
        per_task_usd=cfg.per_task_usd,
        fleet_usd=cfg.fleet_usd,
        integration_branch=cfg.integration_branch,
    )
