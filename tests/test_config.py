from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.config import ConfigError, ConfigNotFound, load_config
from fleet.cost import UnpricedModel, price_lines

TOML = """
[repo]
slug = "FibonAdithya/fleet-fixture"
integration_branch = "integration"

[policy]
ownership = "docs/agent/ownership.md"
task_classes = "docs/agent/task-classes.md"
roles = ".agents/roles"
schemas = ".agents/schemas"

[fleet]
slots = 6
stall_minutes = 10
max_attempts = 2
max_fix_passes = 1
"""

BASE = (
    '[repo]\nslug = "o/r"\n\n[policy]\n'
    'ownership = "docs/agent/ownership.md"\n'
    'task_classes = "docs/agent/task-classes.md"\n'
    'roles = ".agents/roles"\nschemas = ".agents/schemas"\n'
)


def _repo(tmp_path):
    (tmp_path / "fleet.toml").write_text(TOML)
    (tmp_path / ".agents" / "schemas").mkdir(parents=True)
    (tmp_path / "deep" / "nested").mkdir(parents=True)
    return tmp_path


def test_loads_from_repo_root(tmp_path):
    cfg = load_config(_repo(tmp_path))
    assert cfg.repo_slug == "FibonAdithya/fleet-fixture"
    assert cfg.integration_branch == "integration"
    assert cfg.slots == 6
    assert cfg.max_attempts == 2
    assert cfg.max_fix_passes == 1


def test_walks_up_from_a_subdirectory(tmp_path):
    root = _repo(tmp_path)
    cfg = load_config(root / "deep" / "nested")
    assert cfg.root == root


def test_policy_paths_are_absolute(tmp_path):
    cfg = load_config(_repo(tmp_path))
    assert cfg.schemas_dir.is_absolute()
    assert cfg.schemas_dir == tmp_path / ".agents" / "schemas"


def test_missing_config_raises(tmp_path):
    with pytest.raises(ConfigNotFound):
        load_config(tmp_path)


def test_budget_is_parsed(tmp_path):
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = "r"\nschemas = "s"\n[budget]\nper_task_usd = 3.5\nfleet_usd = 40.0\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.per_task_usd == 3.5
    assert cfg.fleet_usd == 40.0


def test_budget_has_defaults_when_the_block_is_absent(tmp_path):
    """The block is optional, but decide()'s drain rule is not -- a missing
    [budget] must yield a real ceiling, not None, or the drain never fires."""
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = "r"\nschemas = "s"\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.per_task_usd == 2.0
    assert cfg.fleet_usd == 25.0


BUDGET_TOML = (
    '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
    'roles = "r"\nschemas = "s"\n[budget]\nper_task_usd = {}\nfleet_usd = {}\n'
)


def test_max_rebases_defaults_to_one(tmp_path):
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = "r"\nschemas = "s"\n'
    )
    assert load_config(tmp_path).max_rebases == 1


def test_max_rebases_is_read_from_fleet_toml(tmp_path):
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = "r"\nschemas = "s"\n[fleet]\nmax_rebases = 3\n'
    )
    assert load_config(tmp_path).max_rebases == 3


def test_a_zero_fleet_budget_is_refused(tmp_path):
    """decide() used to guard the drain with `fleet_usd > 0`, so zero meant the
    rule never fired and an overnight run had no fleet-wide ceiling at all -- a
    misconfiguration silently becoming unlimited spend."""
    (tmp_path / "fleet.toml").write_text(BUDGET_TOML.format("2.0", "0"))
    with pytest.raises(ConfigError, match="fleet_usd"):
        load_config(tmp_path)


def test_a_non_positive_per_task_budget_is_refused(tmp_path):
    """The mirror failure: every agent is over budget on its first tick, so the
    whole backlog is released and then escalated."""
    (tmp_path / "fleet.toml").write_text(BUDGET_TOML.format("-1.0", "25.0"))
    with pytest.raises(ConfigError, match="per_task_usd"):
        load_config(tmp_path)


def test_prices_are_parsed_per_model(tmp_path):
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = "r"\nschemas = "s"\n'
        '[prices."claude-opus-4"]\n'
        "input = 15.0\noutput = 75.0\ncache_read = 1.5\ncache_write = 18.75\n"
    )
    prices = load_config(tmp_path).prices
    assert prices["claude-opus-4"]["cache_write"] == 18.75


def test_a_price_table_missing_a_counter_is_refused(tmp_path):
    """Three of four rates present prices the fourth at nothing, which is the
    same silent hole as an unpriced model -- and cache reads are most of the
    tokens an agent spends."""
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = "r"\nschemas = "s"\n'
        '[prices."claude-opus-4"]\ninput = 15.0\noutput = 75.0\ncache_read = 1.5\n'
    )
    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path)
    assert "cache_write" in str(caught.value)


def test_no_prices_table_parses_but_leaves_the_daemon_unable_to_spend(tmp_path):
    """Two halves, and the second is the one that was missing.

    Config still parses: `fleet plan` starts no agents and needs no prices, and
    requiring the table would refuse every fleet.toml written before it existed.

    But an empty mapping is not permission to run unpriced. It prices NOTHING,
    so the daemon must refuse the same way it refuses an unknown model --
    `UnpricedModel`, recorded in `unpriced()`, which `Daemon.tick` turns into a
    Drain. The old assertion stopped at `prices == {}`, and underneath it
    `parse_snapshot`'s `and prices` guard skipped the join entirely: every
    `cost_usd` read 0.0, `unpriced()` stayed empty, the drain never armed and
    `per_task_usd` could never fire. A whole night with the budget ceiling
    switched off, through the one shape nobody wrote a case for.
    """
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = "r"\nschemas = "s"\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.prices == {}
    with pytest.raises(UnpricedModel):
        price_lines(
            [json.dumps({"message": {"model": "claude-opus-4", "usage": {"input_tokens": 1}}})],
            cfg.prices,
        )


def test_daemon_paths_default_relative_to_root_and_home(tmp_path):
    """No `[daemon]` table at all: socket and run log default under the repo's
    own `.fleet/`, and the worktree root defaults under the user's home --
    the fixture repo should not grow a directory of scratch worktrees that
    `git status` then reports as untracked."""
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = "r"\nschemas = "s"\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.socket_path == tmp_path / ".fleet" / "fleet.sock"
    assert cfg.runlog_path == tmp_path / ".fleet" / "run.jsonl"
    assert cfg.worktree_root == Path("~/.fleet/worktrees").expanduser()
    assert cfg.poll_seconds == 60


def test_daemon_paths_are_read_from_fleet_toml_and_tilde_is_expanded(tmp_path):
    """An unexpanded `~` becomes a literal directory named `~` under the repo
    root, which is the kind of thing nobody notices until a worktree turns up
    in `git status`."""
    (tmp_path / "fleet.toml").write_text(
        '[repo]\nslug = "o/r"\n[policy]\nownership = "o.md"\ntask_classes = "t.md"\n'
        'roles = "r"\nschemas = "s"\n'
        '[daemon]\nsocket = "~/custom/fleet.sock"\nrunlog = "~/custom/run.jsonl"\n'
        'worktrees = "~/custom/worktrees"\npoll_seconds = 15\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.socket_path == Path("~/custom/fleet.sock").expanduser()
    assert cfg.runlog_path == Path("~/custom/run.jsonl").expanduser()
    assert cfg.worktree_root == Path("~/custom/worktrees").expanduser()
    assert cfg.poll_seconds == 15
    for path in (cfg.socket_path, cfg.runlog_path, cfg.worktree_root):
        assert "~" not in str(path)


def test_agents_section_is_parsed_per_kind(tmp_path):
    """Launch flags are CLI-specific: `--permission-mode acceptEdits` means
    nothing to codex. Catches one global arg list shared across kinds."""
    (tmp_path / "fleet.toml").write_text(
        BASE
        + '\n[agents.claude]\nmodel_flag = "--model"\nargs = ["--permission-mode", "acceptEdits"]\n'
        + '\n[agents.codex]\nmodel_flag = "--model"\nargs = ["--full-auto"]\n'
    )
    agents = load_config(tmp_path).agents
    assert agents["codex"].args == ("--full-auto",)
    assert agents["claude"].model_flag == "--model"


def test_an_agent_without_a_model_flag_is_refused(tmp_path):
    """The model is always appended as [model_flag, model]. Without a flag the
    agent would silently run the operator's default model -- the exact
    silent-success the declared-model design exists to close."""
    (tmp_path / "fleet.toml").write_text(BASE + '\n[agents.codex]\nargs = ["--full-auto"]\n')
    with pytest.raises(ConfigError, match="model_flag"):
        load_config(tmp_path)
