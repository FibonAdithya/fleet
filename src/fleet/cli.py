"""The `fleet` command line. Agents never call `gh` directly; they call this."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import click

from fleet import git
from fleet.commands import do_block, do_discover, do_escalate, do_handoff
from fleet.config import load_config
from fleet.gh import RealGh


def _context() -> tuple:
    cfg = load_config()
    gh = RealGh(cfg.repo_slug)
    agent = os.environ.get("FLEET_AGENT")
    if agent is None:
        # "manual" is legitimate for a human running this by hand, which makes it
        # indistinguishable from a dispatch bug that never exported the variable.
        # Say so on stderr rather than silently misattributing an unattended run.
        click.echo("warning: FLEET_AGENT unset; attributing to 'manual'", err=True)
        agent = "manual"
    identity = {
        "role": os.environ.get("FLEET_ROLE", "implementer"),
        "agent": agent,
        "run": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return cfg, gh, identity


def _git_facts(cfg) -> dict:
    return {
        "base_sha": git.merge_base_sha(cfg.root, cfg.integration_branch),
        "head_sha": git.head_sha(cfg.root),
        "branch": git.branch_name(cfg.root),
    }


@click.group()
def main() -> None:
    """Fleet: parallel agents with explicit handoffs."""


@main.command()
@click.option("--task", type=int, required=True)
@click.option(
    "--to", required=True, type=click.Choice(["review", "claimed", "integrating", "done"])
)
@click.option("--did", required=True, help="What you actually changed, specifically.")
@click.option("--checks", required=True, help='e.g. "make check: pass"')
@click.option("--command", "commands", multiple=True, required=True)
@click.option("--open-question", "open_questions", multiple=True)
@click.option("--do-not", "do_not", multiple=True)
@click.option("--discovered", multiple=True, type=int)
def handoff(task, to, did, checks, commands, open_questions, do_not, discovered):
    """Hand this task to the next stage."""
    cfg, gh, identity = _context()
    do_handoff(
        cfg,
        gh,
        task=task,
        to=to,
        did=did,
        checks=checks,
        commands=list(commands),
        open_questions=list(open_questions),
        do_not=list(do_not),
        discovered=list(discovered),
        **identity,
        **_git_facts(cfg),
    )
    click.echo(f"handed task {task} to {to}")


@main.command()
@click.option("--task", type=int, required=True)
@click.option("--on", type=int, required=True, help="The task number this one waits for.")
@click.option("--did", required=True)
@click.option("--checks", default="not run: blocked")
@click.option("--command", "commands", multiple=True, default=["make check"])
def block(task, on, did, checks, commands):
    """Block on another task. Resolves itself; no human is woken."""
    cfg, gh, identity = _context()
    do_block(
        cfg,
        gh,
        task=task,
        on=on,
        did=did,
        checks=checks,
        commands=list(commands),
        **identity,
        **_git_facts(cfg),
    )
    click.echo(f"task {task} blocked on {on}")


@main.command()
@click.option("--task", type=int, required=True)
@click.option("--question", required=True, help="The specific question a human must answer.")
@click.option("--considered", required=True, help="The options you already weighed.")
@click.option("--did", required=True)
@click.option("--checks", default="not run: escalated")
@click.option("--command", "commands", multiple=True, default=["make check"])
def escalate(task, question, considered, did, checks, commands):
    """Stop and ask a human. Terminal until acted on."""
    cfg, gh, identity = _context()
    do_escalate(
        cfg,
        gh,
        task=task,
        question=question,
        considered=considered,
        did=did,
        checks=checks,
        commands=list(commands),
        **identity,
        **_git_facts(cfg),
    )
    click.echo(f"task {task} escalated to a human")


@main.command()
@click.option("--from", "parent", type=int, required=True, help="The task you are working on.")
@click.option("--title", required=True)
@click.option("--area", required=True, help="One of: eval train models data configs docs")
@click.option("--evidence", required=True, help="file:line and what is wrong there.")
def discover(parent, title, area, evidence):
    """File an unrelated defect for someone else. Then carry on with your task."""
    cfg, gh, _ = _context()
    number = do_discover(cfg, gh, parent=parent, title=title, area=area, evidence=evidence)
    click.echo(f"filed #{number} in triage (discovered by #{parent})")


if __name__ == "__main__":
    sys.exit(main())
