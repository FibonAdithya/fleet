"""The `fleet` command line. Agents never call `gh` directly; they call this."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import click

from fleet import git, labels, statesocket
from fleet import policy as policy_module
from fleet import runlog as runlog_module
from fleet import status as status_module
from fleet.commands import do_block, do_discover, do_escalate, do_handoff, do_verdict
from fleet.config import load_config
from fleet.daemon import Daemon, stop_on_signals
from fleet.decide import decide
from fleet.gh import RealGh
from fleet.herdr import RealHerdr
from fleet.herdr_executor import HerdrExecutor
from fleet.model import FleetState
from fleet.reader import read_ledger
from fleet.runlog import RunLog
from fleet.simulate import render_intent

#: Seconds `gh api user` may take. One tiny call, made once at startup before
#: the first tick, and its failure already answers None -- so this only bounds
#: how long `fleet run` can sit saying nothing before it starts.
GH_LOGIN_TIMEOUT_SECONDS = 30

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

    Read through `reader.read_ledger`, the daemon's own ledger read, and not
    through `gh.list_issues("open")`. A preview that disagrees with the thing it
    previews is worse than no preview, and this one disagreed in three
    directions at once. `Task.blocked_on` comes from a handoff packet and
    nothing else, so with no packets fetched every one was None and UNBLOCK was
    unreachable; `_is_rebase_handback` reads the packet too, so an integrator's
    handback rendered as `Release(no_agent)` where the daemon emits `Rebase`;
    and an open-only query drops the closed dependency that satisfies an
    UNBLOCK. Task 21 uses this command as the pre-flight check before a graded
    run, which is the worst possible place for a systematically different
    answer.

    The cost is `gh issue view` per row in a packet-carrying state, which is
    what the daemon already pays every tick.

    Separate from the command because inside it -- behind `RealGh(cfg.repo_slug)`
    -- the only wiring of read_ledger, policy.load and decide() in the CLI is
    reachable by no test that does not talk to GitHub.
    """
    tasks = read_ledger(gh)
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
def plan():
    """Print what the allocator would do right now. Reads only; writes nothing.

    Every row is read through the daemon's own ledger read, so this is the same
    answer the next tick would give.
    """
    cfg = load_config()
    # No `--no-dry-run`. It selected an unwrapped, writing client for a body
    # that makes no write at all -- `do_plan` reads and renders -- so the flag
    # was accepted and did nothing, which is worse than either refusing or
    # writing: an operator who passes it believes something happened. The test
    # that claimed to pin it monkeypatched `do_plan` away and so could never
    # have seen that. The command previews; the wrapper is what makes "writes
    # nothing" a property of the code path rather than of having read it.
    gh = ReadOnlyGh(RealGh(cfg.repo_slug))
    for line in do_plan(cfg, gh, datetime.now(timezone.utc)):
        click.echo(line)


#: The minted run id's shape. Sortable, filename-safe, and second-resolution --
#: two daemons started in the same second would share a night, which is what
#: `--run-id` and `--new-run` exist to settle explicitly.
RUN_ID_FORMAT = "%Y-%m-%dT%H-%M-%SZ"


def resolve_run_id(
    runlog_path: Path, run_id: str | None, new_run: bool, now: datetime
) -> tuple[str, str]:
    """The night this process belongs to, and how it decided. Never guesses.

    Resuming is the default because the alternative is worse in the direction
    that costs money. `Daemon` recovers `spent_usd` by summing the run log for
    ITS run id, so a freshly minted id at every start filters on an id that
    appears in zero lines and resumes at zero -- a 2am restart hands the fleet a
    second full budget and parent §10's drain never fires. That is spec §8's
    named failure, and it was reachable through the only command a human uses
    to start a daemon.

    The cost of resuming by default is the mirror case: last night's id is still
    the last line in the log tonight, so tonight would start already drained.
    That is loud and safe -- the fleet dispatches nothing and says why -- and
    `--new-run` is one flag away, which is not true of a night quietly spending
    twice its ceiling.

    Returns the id and a phrase naming the choice, because the operator has to
    be able to read which of the three happened off one line at 2am.
    """
    if run_id and new_run:
        raise click.ClickException(
            "--run-id and --new-run contradict each other: one names a night to "
            "rejoin, the other starts a new one. Pass exactly one."
        )
    if run_id:
        return run_id, "given on the command line"
    if not new_run:
        previous = runlog_module.latest_run_id(runlog_path)
        if previous:
            return previous, "resumed from the run log"
    return now.strftime(RUN_ID_FORMAT), "new run"


@main.command()
@click.option("--once", is_flag=True, help="One tick, then exit. For a supervised slice.")
@click.option("--poll-seconds", type=int, default=None)
@click.option(
    "--run-id",
    default=None,
    help="Rejoin this night by name, whatever the run log's last line says.",
)
@click.option(
    "--new-run",
    is_flag=True,
    help="Start a new night rather than resuming the run log's last one. "
    "Without it, a restart resumes -- and inherits what that night already spent.",
)
def run(once: bool, poll_seconds: int | None, run_id: str | None, new_run: bool) -> None:
    """Start the allocator daemon."""
    cfg = load_config()
    policy = policy_module.load(cfg)
    gh = RealGh(cfg.repo_slug)
    herdr = RealHerdr(
        worktree_root=cfg.worktree_root,
        projects_dir=Path.home() / ".claude" / "projects",
        sessions_dir=Path.home() / ".codex" / "sessions",
        prices=cfg.prices,
        repo_root=cfg.root,
        agents=cfg.agents,
        routing={role: (kind, model) for role, kind, model in policy.agents},
    )
    owner = _gh_login()
    executor = HerdrExecutor(herdr, gh, cfg, owner=owner)
    run_id, origin = resolve_run_id(cfg.runlog_path, run_id, new_run, datetime.now(timezone.utc))
    log = RunLog(cfg.runlog_path, run_id=run_id)
    daemon = Daemon(cfg, policy, gh, executor, log, run_id=run_id)

    stop = threading.Event()
    # PULL-based: the socket calls back into the daemon's own last snapshot on
    # every connection, so this is wired directly to the attribute rather than
    # through `Daemon.run`'s `publish` callback -- that callback is a push and
    # does not fit a client that connects, reads once, and disconnects.
    thread = statesocket.serve(cfg.socket_path, lambda: daemon.snapshot, stop)
    # One line, three facts: which night this is, how that was decided, and what
    # this process believes that night has already spent. The third is the one
    # that was unreadable before -- a daemon resuming at $0.00 against a night
    # that spent $20 looks exactly like a daemon starting a fresh night, and the
    # difference is the whole of the fleet ceiling.
    click.echo(
        f"fleet run: run_id={run_id} ({origin}) "
        f"spent=${daemon.state.spent_usd:.2f} socket={cfg.socket_path}"
    )
    try:
        # SIGTERM as well as SIGINT: a supervisor stopping the daemon must get
        # the same clean shutdown a Ctrl-C gives, or the socket file is left
        # behind. `stop_on_signals` installs both and restores the previous
        # handlers on the way out.
        with stop_on_signals(stop):
            if once:
                daemon.tick(datetime.now(timezone.utc))
                click.echo("\n".join(status_module.render(daemon.snapshot.to_json())))
            else:
                daemon.run(cfg.poll_seconds if poll_seconds is None else poll_seconds, stop)
    finally:
        stop.set()
        thread.join(timeout=2)


@main.command()
def status() -> None:
    """Print the last completed tick."""
    cfg = load_config()
    try:
        payload = statesocket.request(cfg.socket_path)
    except statesocket.NotRunning as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("\n".join(status_module.render(payload)))


def _gh_login() -> str | None:
    """The login `gh` is authenticated as -- the fleet's own identity.

    Parent §3 makes the allocator the sole writer of ownership, and `fleet-1` is
    not a GitHub login: gh fails the whole edit on an assignee it cannot resolve.
    None rather than a guess if gh cannot say, because a wrong assignee fails
    every label edit that rides along with it.
    """
    try:
        return (
            subprocess.run(
                ["gh", "api", "user", "--jq", ".login"],
                capture_output=True,
                text=True,
                check=True,
                timeout=GH_LOGIN_TIMEOUT_SECONDS,
            ).stdout.strip()
            or None
        )
    # TimeoutExpired is neither an OSError nor a CalledProcessError, so without
    # it here a wedged `gh` at startup takes `fleet run` down with a traceback
    # rather than falling to the None this handler already means.
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


if __name__ == "__main__":
    sys.exit(main())
