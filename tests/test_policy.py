from __future__ import annotations

import pytest

from conftest import FIXTURE_POLICY
from fleet import policy as policy_module
from fleet.config import load_config
from fleet.model import Policy
from fleet.policy import (
    PolicyError,
    area_names,
    known_roles,
    load,
    parse_agents,
    parse_ownership,
    parse_routes,
)

OWNERSHIP = """# Ownership map

| Glob | Area |
|---|---|
| `src/eval/**` | eval |
| `src/train/**` | train |
| `gates/**` | configs |

## Non-dispatchable paths

| Path | Why |
|---|---|
| `gates/**` | human judgement |
"""

CLASSES = """# Task classes

| Class | Route | Output | Unattended? |
|---|---|---|---|
| `patch` | implementer → reviewer → integrator | merged commit | yes |
| `spec` | planner | a spec and child issues | yes |
"""

ROLES = frozenset(
    {"chain", "implementer", "integrator", "invariant-reviewer", "planner", "reviewer", "scout"}
)


def test_ownership_reads_the_glob_table():
    """Catches reading the wrong column, and catches leaving the backticks on:
    a glob of "`src/eval/**`" matches no path fnmatch is ever given."""
    assert parse_ownership(OWNERSHIP) == (
        ("src/eval/**", "eval"),
        ("src/train/**", "train"),
        ("gates/**", "configs"),
    )


def test_ownership_ignores_the_second_table():
    """Catches a reader that takes the first table, or every table. The file
    carries a Non-dispatchable table headed Path | Why; reading it as globs maps
    `gates/**` to the area "human judgement", which then fails the AREAS check
    for a reason that names the wrong table."""
    areas = dict(parse_ownership(OWNERSHIP))
    assert areas["gates/**"] == "configs"


def test_an_empty_ownership_table_raises():
    """Catches returning (). A silently-empty map means no area exclusion, so
    every task looks dispatchable and six agents run into one area -- a failure
    that passes every test not specifically looking for it."""
    with pytest.raises(PolicyError, match="no ownership rules"):
        parse_ownership("# Ownership map\n\nNothing here.\n")


def test_a_malformed_area_name_in_the_ownership_map_raises():
    """Areas now come from this table, so the only check left is that the name
    can be a label. Catches dropping it: "Not An Area" would become the label
    `area:Not An Area`, which gh accepts and no task ever carries."""
    with pytest.raises(PolicyError, match="invalid area name"):
        parse_ownership("| Glob | Area |\n|---|---|\n| `src/x/**` | Not An Area |\n")


def test_areas_outside_the_fixture_vocabulary_are_accepted():
    """The fleet repository's own areas (allocator, ledger, ...) are not the
    fixture's. Catches keeping the old membership check."""
    pairs = parse_ownership(
        "| Glob | Area |\n|---|---|\n"
        "| `src/fleet/decide.py` | allocator |\n| `README.md` | docs |\n"
    )
    assert {area for _, area in pairs} == {"allocator", "docs"}


def test_area_names_reads_only_the_ownership_map(cfg):
    """`area_names` must work from ownership.md alone: the box's file_task verb
    calls it in a target repo, and the fixture cfg has no task-classes or
    roles. Catches implementing it as `load(cfg).area_names`."""
    cfg.ownership.write_text(
        "| Glob | Area |\n|---|---|\n| `a/**` | alpha |\n| `b/**` | beta |\n| `c/**` | alpha |\n"
    )
    assert area_names(cfg) == frozenset({"alpha", "beta"})


def test_routes_split_on_the_arrow():
    """Catches splitting on "->" instead of U+2192, and catches not splitting at
    all: the whole cell would become a single role name."""
    assert dict(parse_routes(CLASSES, ROLES))["patch"] == (
        "implementer",
        "reviewer",
        "integrator",
    )
    assert dict(parse_routes(CLASSES, ROLES))["spec"] == ("planner",)


def test_the_real_investigation_route_keeps_only_its_leading_role():
    """The reason this module reads the vendored file and not just a synthetic
    constant. task-classes.md's investigation cell is
    `implementer, forbidden from writing `src/``, which contains no arrow -- so a
    parser that only splits on the arrow yields that entire sentence as the role,
    hands it to StartAgent, and asks herdr for a role prompt that does not exist.
    Captured issue #10 is class:investigation, so every scenario seeded from the
    real ledger dispatches it."""
    text = (FIXTURE_POLICY / "docs/agent/task-classes.md").read_text()
    routes = dict(parse_routes(text, ROLES))
    assert routes["investigation"] == ("implementer",)
    assert routes["integration"] == ("integrator",)
    assert routes["patch"] == ("implementer", "reviewer", "integrator")


def test_a_route_naming_an_unknown_role_raises():
    """Catches relaxing the role check to salvage the prose cell above. Dropping
    the prose is deliberate; dropping the check is how the garbage role name got
    through in the first place, so the strictness spec §9 asks for has to survive
    the same edit."""
    with pytest.raises(PolicyError, match="unknown role"):
        parse_routes("| Class | Route |\n|---|---|\n| `patch` | implementer → revewier |\n", ROLES)


def test_a_route_that_is_only_prose_raises():
    """Catches treating the whole cell as prose to be skipped. A class whose
    route names no role is not routable, and returning () for it would make
    `_dispatch` silently skip every task of that class forever."""
    with pytest.raises(PolicyError, match="unknown role"):
        parse_routes("| Class | Route |\n|---|---|\n| `patch` | handled by a human |\n", ROLES)


def test_an_unknown_class_raises():
    """Catches dropping the CLASSES check: a class no label exists for can never
    be carried by a task, so its route is unreachable and its absence silent."""
    with pytest.raises(PolicyError, match="unknown class"):
        parse_routes("| Class | Route |\n|---|---|\n| `path` | implementer |\n", ROLES)


def test_an_empty_class_table_raises():
    """Catches returning (): route_for would then be () for every class and
    nothing would ever be dispatched, with no error to say why."""
    with pytest.raises(PolicyError, match="no task classes"):
        parse_routes("# Task classes\n\nNothing.\n", ROLES)


def test_the_roles_directory_is_the_role_vocabulary():
    """Catches reading role names from anywhere but the target repo's own prompt
    files -- a hardcoded list here would validate routes against fleet's opinion
    of what roles exist rather than the repo's."""
    assert known_roles(FIXTURE_POLICY / ".agents/roles") == ROLES


def test_a_missing_roles_directory_raises(tmp_path):
    """Catches returning an empty set. An empty vocabulary makes every role check
    vacuous, so a typo'd role and a prose cell both validate -- exactly the
    permissive path this module exists to close."""
    with pytest.raises(PolicyError, match="no role prompts"):
        known_roles(tmp_path / "nope")


def test_policy_carries_max_rebases_from_config(cfg, tmp_path):
    """A cap that stops at FleetConfig is a cap decide() cannot enforce."""
    (tmp_path / "o.md").write_text("| Glob | Area |\n|---|---|\n| `src/eval/**` | `eval` |\n")
    (tmp_path / "t.md").write_text(
        "| Class | Route | Output | Unattended? |\n|---|---|---|---|\n"
        "| `patch` | implementer → reviewer → integrator | c | yes |\n\n"
        "| Role | Agent | Model |\n|---|---|---|\n"
        "| `implementer` | `claude` | `m` |\n"
        "| `reviewer` | `claude` | `m` |\n"
        "| `integrator` | `claude` | `m` |\n"
    )
    (tmp_path / ".agents" / "roles").mkdir(parents=True)
    for role in ("implementer", "reviewer", "integrator"):
        (tmp_path / ".agents" / "roles" / f"{role}.md").write_text("x")
    with (tmp_path / "fleet.toml").open("a") as f:
        f.write(
            '\n[agents.claude]\nmodel_flag = "--model"\nargs = []\n\n'
            '[prices."m"]\ninput = 1.0\noutput = 1.0\ncache_read = 1.0\ncache_write = 1.0\n'
        )
    loaded = policy_module.load(load_config(tmp_path))
    assert loaded.max_rebases == 1


def test_load_reads_the_vendored_fixture_policy():
    """End to end against the real captured policy layer, not a synthetic one.
    Catches a `load` that drops integration_branch (dispatching every agent off
    the wrong base) or that forgets to pass the role vocabulary to parse_routes."""
    cfg = load_config(FIXTURE_POLICY)
    policy = load(cfg)
    assert policy.slots == 6
    assert policy.stall_minutes == 10
    assert policy.max_attempts == 2
    assert policy.max_fix_passes == 1
    assert policy.per_task_usd == 2.0
    assert policy.fleet_usd == 25.0
    assert policy.integration_branch == "integration"
    assert policy.route_for("patch") == ("implementer", "reviewer", "integrator")
    assert policy.route_for("investigation") == ("implementer",)
    assert dict(policy.areas)["src/eval/**"] == "eval"
    assert dict(policy.areas)["gates/**"] == "configs"


# A local name distinct from the module-level ROLES above: the whole module
# executes before any test runs, so reusing ROLES here would silently rebind
# the constant every earlier test in this file reads, shrinking their role
# vocabulary out from under them.
AGENT_ROLES = frozenset({"implementer", "reviewer", "integrator", "planner"})

AGENT_TABLE = """
| Role | Agent | Model |
|---|---|---|
| `implementer` | `codex` | `gpt-5.6-sol` |
| `reviewer` | `claude` | `claude-opus-5` |
"""


def test_the_agent_table_is_parsed_into_role_agent_model_triples():
    """Catches a parser that reads only two columns and drops the model, which
    would leave every role on the operator's default model."""
    assert parse_agents(AGENT_TABLE, AGENT_ROLES) == (
        ("implementer", "codex", "gpt-5.6-sol"),
        ("reviewer", "claude", "claude-opus-5"),
    )


def test_an_unknown_role_in_the_agent_table_raises():
    """Same strictness parse_routes applies to an unknown class. Catches
    skipping the row, which leaves that role silently unrouted."""
    text = "\n| Role | Agent | Model |\n|---|---|---|\n| `nobody` | `claude` | `m` |\n"
    with pytest.raises(PolicyError, match="nobody"):
        parse_agents(text, AGENT_ROLES)


def test_a_missing_agent_table_raises():
    """Required, not defaulted to all-claude -- the reasoning the Policy
    docstring gives for integration_branch having no default. Catches an empty
    tuple, which would make every downstream lookup return None."""
    with pytest.raises(PolicyError, match="no role/agent"):
        parse_agents("# no tables here\n", AGENT_ROLES)


def test_a_role_listed_twice_raises():
    """Catches last-wins, which silently discards the first assignment."""
    text = (
        "\n| Role | Agent | Model |\n|---|---|---|\n"
        "| `reviewer` | `claude` | `a` |\n| `reviewer` | `codex` | `b` |\n"
    )
    with pytest.raises(PolicyError, match="reviewer"):
        parse_agents(text, AGENT_ROLES)


def _config_with_agent_table(tmp_path, agent: str, model: str):
    """A FleetConfig whose fixture policy declares one role on `agent`/`model`."""
    import shutil

    root = tmp_path / "repo"
    shutil.copytree(FIXTURE_POLICY, root)
    classes = root / "docs/agent/task-classes.md"
    row = f"| `implementer` | `{agent}` | `{model}` |\n"
    classes.write_text(classes.read_text() + f"\n\n| Role | Agent | Model |\n|---|---|---|\n{row}")
    # A fresh fleet.toml, NOT an append. The fixture's fleet.toml already
    # declares [prices."claude-opus-5"], and tomllib rejects a duplicate table
    # outright (`Cannot declare ('prices', 'm') twice`), so appending here
    # would raise TOMLDecodeError.
    (root / "fleet.toml").write_text(
        '[repo]\nslug = "FibonAdithya/fleet-fixture"\n'
        'integration_branch = "integration"\n\n'
        "[policy]\n"
        'ownership = "docs/agent/ownership.md"\n'
        'task_classes = "docs/agent/task-classes.md"\n'
        'roles = ".agents/roles"\nschemas = ".agents/schemas"\n\n'
        '[agents.claude]\nmodel_flag = "--model"\nargs = []\n\n'
        '[prices."claude-opus-5"]\n'
        "input = 5.0\noutput = 25.0\ncache_read = 0.5\ncache_write = 10.0\n"
    )
    return load_config(root)


def test_a_declared_model_with_no_price_is_refused_at_load(tmp_path):
    """docs/m3-inputs.md §1.2: an unpriced model is currently discovered only
    when a dispatched agent's transcript is read, mid-run, after spending.
    Catches deferring that to runtime now that the model is declared."""
    cfg = _config_with_agent_table(tmp_path, agent="claude", model="unpriced-model")
    with pytest.raises(PolicyError, match="unpriced-model"):
        policy_module.load(cfg)


def test_a_declared_agent_with_no_provider_is_refused_at_load(tmp_path):
    """Catches routing a role to a CLI whose transcripts nothing can read,
    which would drain on the first tick instead of failing at startup."""
    cfg = _config_with_agent_table(tmp_path, agent="nosuchcli", model="claude-opus-5")
    with pytest.raises(PolicyError, match="nosuchcli"):
        policy_module.load(cfg)


def test_a_declared_kind_with_no_launch_config_is_refused_at_load(tmp_path):
    """FINDING 1(a). Catches dropping the `kind not in agent_specs` check out of
    `_validate_agents`. Without it, a role routed to `claude` -- a real
    transcript-reading, priced kind -- still passes startup when fleet.toml
    carries no `[agents.claude]` launch config. HerdrExecutor._start then
    raises HerdrError for every task of that role, `_begin` swallows it, the
    next tick's REAP releases the claimed row and burns an attempt, and the
    whole backlog walks to NEEDS_HUMAN overnight having launched nothing."""
    import shutil

    root = tmp_path / "repo"
    shutil.copytree(FIXTURE_POLICY, root)
    # The vendored fixture's fleet.toml declares [agents.claude]; this one
    # deliberately does not, while still declaring the price every role's
    # model needs so the price check does not fire first.
    (root / "fleet.toml").write_text(
        '[repo]\nslug = "FibonAdithya/fleet-fixture"\n'
        'integration_branch = "integration"\n\n'
        "[policy]\n"
        'ownership = "docs/agent/ownership.md"\n'
        'task_classes = "docs/agent/task-classes.md"\n'
        'roles = ".agents/roles"\nschemas = ".agents/schemas"\n\n'
        '[prices."claude-opus-5"]\n'
        "input = 5.0\noutput = 25.0\ncache_read = 0.5\ncache_write = 10.0\n"
    )
    cfg = load_config(root)
    with pytest.raises(PolicyError, match=r"\[agents\.claude\]"):
        policy_module.load(cfg)


def test_a_role_missing_from_the_agent_table_is_refused_at_load(tmp_path):
    """FINDING 1(b). Catches dropping the routes-coverage loop out of
    `_validate_agents`. Without it, a role a route names but the
    `Role | Agent | Model` table omits still passes startup: dispatch fails
    at HerdrExecutor._start exactly as (a) does, and worse --
    `Policy.agent_for('implementer')` returns None, so `_model_mismatch`
    silently stops checking that role's model at all, with nothing said."""
    import shutil

    root = tmp_path / "repo"
    shutil.copytree(FIXTURE_POLICY, root)
    classes = root / "docs/agent/task-classes.md"
    text = classes.read_text()
    row = "| `implementer` | `claude` | `claude-opus-5` |\n"
    assert row in text, "fixture shape changed; this test's precondition no longer holds"
    classes.write_text(text.replace(row, ""))
    cfg = load_config(root)
    with pytest.raises(PolicyError, match="implementer"):
        policy_module.load(cfg)


_FIXTURE_TOML_HEAD = (
    '[repo]\nslug = "FibonAdithya/fleet-fixture"\n'
    'integration_branch = "integration"\n\n'
    "[policy]\n"
    'ownership = "docs/agent/ownership.md"\n'
    'task_classes = "docs/agent/task-classes.md"\n'
    'roles = ".agents/roles"\nschemas = ".agents/schemas"\n\n'
    '[agents.claude]\nmodel_flag = "--model"\nargs = []\n\n'
)

_OPUS_PRICE = (
    '[prices."claude-opus-5"]\ninput = 5.0\noutput = 25.0\ncache_read = 0.5\ncache_write = 10.0\n'
)


def _config_with_chain_role(
    tmp_path,
    model: str = "chain",
    chain_table: str = '[agents.chain]\ncommand = "/usr/bin/chain"\nper_task_usd = 40.0\n',
    extra: str = "",
    prices: str = _OPUS_PRICE,
):
    """The vendored fixture plus a `chain` role on kind `chain`, routed from
    class `build`. No [prices] row for the chain model on purpose."""
    import shutil

    root = tmp_path / "repo"
    shutil.copytree(FIXTURE_POLICY, root)
    (root / ".agents/roles/chain.md").write_text("x")
    classes = root / "docs/agent/task-classes.md"
    text = classes.read_text()
    # The vendored fixture now carries the chain role, the build/bug routes and
    # the chain agent row itself; strip them so this helper controls each one.
    for fixture_row in (
        "| `build` | chain → integrator | merged commit | yes |\n",
        "| `bug` | chain → integrator | merged commit | yes |\n",
        "| `chain` | `chain` | `chain` |\n",
    ):
        assert fixture_row in text, (
            "fixture shape changed; this helper's precondition no longer holds"
        )
        text = text.replace(fixture_row, "")
    text = text.replace(
        "| `integration` | integrator | `integration` advanced | yes |\n",
        "| `integration` | integrator | `integration` advanced | yes |\n"
        "| `build` | chain | merged PR | yes |\n",
    )
    anchor = "| `invariant-reviewer` | `claude` | `claude-opus-5` |\n"
    assert anchor in text, "fixture shape changed; this helper's precondition no longer holds"
    text = text.replace(anchor, anchor + f"| `chain` | `chain` | `{model}` |\n")
    classes.write_text(text)
    (root / "fleet.toml").write_text(_FIXTURE_TOML_HEAD + chain_table + "\n" + prices + extra)
    return load_config(root)


def test_chain_kind_needs_no_price_row(tmp_path):
    """Chain prices its own runs; there is no transcript reader for it and no
    per-token rate. Catches the reader and price checks still applied to kind
    `chain`, which would make the kind impossible to declare."""
    loaded = policy_module.load(_config_with_chain_role(tmp_path))
    assert loaded.agent_for("chain") == ("chain", "chain")
    assert loaded.route_for("build") == ("chain",)


def test_chain_kind_model_cell_must_be_chain(tmp_path):
    """A model name in the chain row would read as a declaration fleet checks,
    and it checks nothing for this kind. Catches accepting any model cell."""
    cfg = _config_with_chain_role(tmp_path, model="claude-opus-5")
    with pytest.raises(PolicyError, match="Model cell"):
        policy_module.load(cfg)


def test_chain_kind_still_needs_a_launch_config(tmp_path):
    """Catches exempting kind `chain` from the [agents.<kind>] check along with
    the reader and price checks."""
    cfg = _config_with_chain_role(tmp_path, chain_table="")
    with pytest.raises(PolicyError, match=r"\[agents\.chain\]"):
        policy_module.load(cfg)


def test_claude_kind_still_needs_price_row(tmp_path):
    """Unchanged strictness beside a chain row. Catches the chain exemption
    leaking into every kind."""
    cfg = _config_with_chain_role(tmp_path, prices="")
    with pytest.raises(PolicyError, match="claude-opus-5"):
        policy_module.load(cfg)


def test_load_copies_the_chain_ceiling_and_table(tmp_path):
    """decide() sees only the Policy, so a value that stops at FleetConfig is
    a value decide() cannot enforce."""
    cfg = _config_with_chain_role(
        tmp_path, extra='\n[chain]\ndefault_area = "eval"\nmax_age_days = 3\n'
    )
    loaded = policy_module.load(cfg)
    assert loaded.ceiling_for("chain") == 40.0
    assert loaded.ceiling_for("implementer") == 2.0
    assert loaded.chain_default_area == "eval"
    assert loaded.chain_max_age_days == 3


def test_chain_default_area_must_be_an_area(tmp_path):
    """An adopted issue labelled with an area the ownership map does not have
    makes ledger.read_one raise on every tick. Catches skipping the check."""
    cfg = _config_with_chain_role(tmp_path, extra='\n[chain]\ndefault_area = "nowhere"\n')
    with pytest.raises(PolicyError, match="nowhere"):
        policy_module.load(cfg)


def _ceiling_policy() -> Policy:
    return Policy(
        areas=(("src/eval/**", "eval"),),
        routes=(("patch", ("implementer",)), ("build", ("chain",))),
        agents=(("implementer", "claude", "m"), ("chain", "chain", "chain")),
        slots=6,
        stall_minutes=10,
        max_attempts=2,
        max_fix_passes=1,
        max_rebases=1,
        per_task_usd=2.0,
        fleet_usd=25.0,
        integration_branch="integration",
        ceilings=(("chain", 40.0),),
    )


def test_ceiling_for_chain_role():
    assert _ceiling_policy().ceiling_for("chain") == 40.0


def test_ceiling_for_other_role_is_global():
    """Catches a lookup by role name rather than by the role's kind, and a
    ceiling applied to every role once any kind sets one."""
    policy = _ceiling_policy()
    assert policy.ceiling_for("implementer") == 2.0
    assert policy.ceiling_for(None) == 2.0
    assert policy.ceiling_for("nobody") == 2.0


def test_fixture_policy_routes_build_and_bug():
    policy = load(load_config(FIXTURE_POLICY))
    assert policy.route_for("build") == ("chain", "integrator")
    assert policy.route_for("bug") == ("chain", "integrator")
    assert dict((r, (a, m)) for r, a, m in policy.agents)["chain"] == ("chain", "chain")
