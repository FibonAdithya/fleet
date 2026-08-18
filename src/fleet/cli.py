"""The `fleet` command line. Agents never call `gh` directly; they call this."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import click

from fleet import git, labels
from fleet import ledger as ledger_module
from fleet import policy as policy_module
from fleet.commands import do_block, do_discover, do_escalate, do_handoff, do_verdict
from fleet.config import load_config
from fleet.decide import decide
from fleet.gh import RealGh
from fleet.model import FleetState
from fleet.simulate import render_intent

ROLE_UNSET = (
    "FLEET_ROLE is unset. It decides which state transitions you may write, so there is "
    "no safe default: 'implementer' hands out exactly the privilege the gate withholds, "
    "and an empty role reaches the command layer as \"unknown role ''\", which blames "
    "the agent for a variable the dispatcher never exported. Export FLEET_ROLE."
)


# Named here rather than inlined so the test asserts the same string the command
# prints.
NO_AGENTS_CAVEAT = (
    "note: M2 has no agent source, so this tick ran with no live agents. Every "
    "fleet:claimed task therefore reads as ledger drift, and the Release below "
    "is an artefact of that rather than a recommendation."
)


class ReadOnlyGh:
    """Wraps a GhClient and refuses everything that is not a known read.

    Deny by default, not a list of known writes. `close_issue` exists on FakeGh,
    on no Protocol, and mutates -- and a wrapper that enumerates its writes
    passes the next such method straight through to the client. `--dry-run
    writes nothing` is then a property of the code path rather than of having
    read it carefully, which is the same reasoning that made FakeGh refuse
    unknown labels: a stand-in more permissive than the real thing is how M1's
    defects survived the entire offline suite.
    """

    READS = frozenset({"get_issue", "list_issues", "search_titles"})

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        if name in self.READS:
            return getattr(self._inner, name)

        def refuse(*_args, **_kwargs):
            raise PermissionError(f"dry-run: refusing to call {name}")

        return refuse


def do_plan(cfg, gh, now: datetime) -> list[str]:
    """One tick of decide() against a live ledger, rendered.

    Separate from the command because inside it -- behind `RealGh(cfg.repo_slug)`
    -- the only wiring of ledger.read, policy.load and decide() in the CLI is
    reachable by no test that does not talk to GitHub.
    """
    tasks = ledger_module.read(gh.list_issues("open"))
    intents, _ = decide(FleetState(), tasks, policy_module.load(cfg), [], now)
    lines: list[str] = []
    if any(task.state == "claimed" for task in tasks):
        lines.append(NO_AGENTS_CAVEAT)
    lines += [render_intent(intent) for intent in intents]
    return lines or ["no action"]


def _context(*, role_required: bool = True) -> tuple:
    cfg = load_config()
    gh = RealGh(cfg.repo_slug)
    # Falsy, not `is None`: a dispatcher doing `export FLEET_AGENT="$AGENT_ID"`
    # with AGENT_ID unset exports an empty string, which is not None. That would
    # skip the warning and the fallback both, then fail schema validation with
    # "from.agent: '' is too short" -- a complaint about a field the operator
    # never set. Same shape for FLEET_ROLE against the role enum.
    agent = os.environ.get("FLEET_AGENT", "").strip()
    if not agent:
        # "manual" is legitimate for a human running this by hand, which makes it
        # indistinguishable from a dispatch bug that never exported the variable.
        # Say so on stderr rather than silently misattributing an unattended run.
        click.echo("warning: FLEET_AGENT unset; attributing to 'manual'", err=True)
        agent = "manual"
    role = os.environ.get("FLEET_ROLE", "").strip()
    # An unset role is refused HERE and nowhere else, because this is the only
    # place that can still tell "unset" from "deliberately implementer". One line
    # further on the information is gone: the command layer sees a role that
    # looks chosen, and `do_handoff`/`do_verdict` gate on a value nobody set.
    # `role_required=False` is for the one command that records no role at all.
    if role_required and not role:
        raise click.ClickException(ROLE_UNSET)
    identity = {
        "role": role,
        "agent": agent,
        "run": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return cfg, gh, identity


def _git_facts(cfg, *, allow_detached: bool = False) -> dict:
    """`allow_detached` is set only by escalate and block. A handoff needs a real
    branch for the receiver to check out, but escalate and block are the only
    commands that reach a human, and a detached HEAD -- mid-bisect, mid-failed-
    rebase -- is exactly when an agent needs them. Refusing there would leave it
    with no way out at all."""
    return {
        "base_sha": git.merge_base_sha(cfg.root, cfg.integration_branch),
        "head_sha": git.head_sha(cfg.root),
        "branch": git.branch_name(cfg.root, allow_detached=allow_detached),
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
        **_git_facts(cfg, allow_detached=True),
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
        **_git_facts(cfg, allow_detached=True),
    )
    click.echo(f"task {task} escalated to a human")


@main.command()
@click.option("--from", "parent", type=int, required=True, help="The task you are working on.")
@click.option("--title", required=True)
@click.option("--area", required=True, type=click.Choice(labels.AREAS))
@click.option("--evidence", required=True, help="file:line and what is wrong there.")
def discover(parent, title, area, evidence):
    """File an unrelated defect for someone else. Then carry on with your task."""
    # The only command that records no role: do_discover ignores identity, and a
    # discovery lands in TRIAGE whoever files it. Requiring FLEET_ROLE here would
    # refuse a human triaging by hand and gate nothing.
    cfg, gh, _ = _context(role_required=False)
    number = do_discover(cfg, gh, parent=parent, title=title, area=area, evidence=evidence)
    click.echo(f"filed #{number} in triage (discovered by #{parent})")


@main.command()
@click.option("--task", type=int, required=True)
@click.option(
    "--verdict", "decision", required=True, type=click.Choice(["approved", "changes_required"])
)
@click.option("--head-sha", required=True, help="The head you actually reviewed.")
@click.option("--checks-rerun", default=None, help='e.g. "make check: pass"')
@click.option(
    "--finding",
    "findings",
    multiple=True,
    help="severity:detail, e.g. must_fix:no test binds the fix",
)
@click.option("--packet-was-sufficient/--packet-was-insufficient", default=None)
def verdict(task, decision, head_sha, checks_rerun, findings, packet_was_sufficient):
    """Record a review verdict. The only legal exit from REVIEW."""
    cfg, gh, identity = _context()
    parsed = []
    for raw in findings:
        # partition, not split: a detail routinely contains a colon
        # ("src/eval/lid.py:12 clamps the wrong bound") and split would truncate
        # it. The schema's optional `path` is deliberately not a third field for
        # the same reason -- it would make that detail ambiguous.
        severity, _, detail = raw.partition(":")
        if not detail.strip():
            raise click.ClickException(f"finding {raw!r} has no detail; use severity:detail")
        parsed.append({"severity": severity.strip(), "detail": detail.strip()})
    _, landed = do_verdict(
        cfg,
        gh,
        task=task,
        verdict=decision,
        head_sha=head_sha,
        reviewer=identity["agent"],
        role=identity["role"],
        findings=parsed,
        checks_rerun=checks_rerun,
        packet_was_sufficient=packet_was_sufficient,
    )
    click.echo(f"recorded {decision} on task {task}; it is now fleet:{landed}")


@main.command()
@click.option("--dry-run/--no-dry-run", default=True, help="Print the plan; write nothing.")
def plan(dry_run):
    """Print what the allocator would do right now."""
    if not dry_run:
        # Refused before load_config, or `fleet plan --no-dry-run` outside a
        # fleet repo complains about a missing fleet.toml instead of saying
        # which half of the flag is unimplemented.
        raise click.ClickException("only --dry-run is implemented; the daemon lands in M3")
    cfg = load_config()
    gh = ReadOnlyGh(RealGh(cfg.repo_slug))
    for line in do_plan(cfg, gh, datetime.now(timezone.utc)):
        click.echo(line)


if __name__ == "__main__":
    sys.exit(main())
