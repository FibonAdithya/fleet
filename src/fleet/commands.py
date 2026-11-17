"""The write path. Every write validates first; a rejected payload writes nothing."""

from __future__ import annotations

import re

from fleet import git as git_module
from fleet import labels
from fleet import policy as policy_module
from fleet.config import FleetConfig
from fleet.decide import chain_route_enabled
from fleet.gh import GhClient
from fleet.ledger import read_one
from fleet.model import Policy
from fleet.packet import build_handoff, render
from fleet.schemas import validate

# Which target states each role may write with a plain handoff. An implementer
# that can reach `done` skips review and integration, and since grading reads
# terminal state from labels, a self-declared `done` scores as rubric-correct.
# This is the mechanical half of parent §4's "give every state one writer".
#
# `reviewer` and `invariant-reviewer` map to the empty set on purpose: spec §5
# ruling 4 makes a verdict REVIEW's only exit, so a reviewer that could hand to
# `claimed` would leave REVIEW without recording a verdict and without spending
# a fix pass -- the counter `decide()` reads to stop a review loop.
# `scout` is empty for a different reason: scout.md's step 5 is "promote to
# fleet:ready", and `ready` is not in handoff.schema.json's `to` enum at all, so
# no handoff can express a scout's write. `planner`'s `review` is a choice, not
# a derivation -- planner.md names no target state, and `review` is the only
# non-terminal member of that enum. Both are recorded in docs/m2-inputs.md §7.
#
# `integrator`'s `claimed` is faithful to integrator.md -- a rebase hands the
# task back -- but nothing starts an agent at that moment, so the next tick
# reads it as ledger drift and spends an attempt. Left as-is in M2 and recorded
# in the same section: routing to `ready` instead is not schema-legal without
# editing the fixture repo.
ROLE_MAY_HAND_TO: dict[str, frozenset[str]] = {
    "implementer": frozenset({"review"}),
    "reviewer": frozenset(),
    "invariant-reviewer": frozenset(),
    "planner": frozenset({"review"}),
    "integrator": frozenset({"done", "claimed"}),
    "scout": frozenset(),
    # chain runs the whole build itself; its hand-off goes to review, and its
    # approved verdict (it is in VERDICT_ROLES) moves the task to integrating.
    "chain": frozenset({"review"}),
}

# Only a reviewing role writes a verdict. `invariant-reviewer` is in because
# invariant-reviewer.md ends "Emit `approved` only if all five are `ok`" -- it
# emits a verdict, on a narrower question. Every other role is the same
# integrity hole ROLE_MAY_HAND_TO closes, arriving by the other door.
VERDICT_ROLES = frozenset({"reviewer", "invariant-reviewer", "chain"})


def _move_state(gh: GhClient, issue: int, state: str, add: list[str] | None = None) -> None:
    """Move the task's state label, carrying any counter label with it.

    Removing all eight and adding one is how the ledger keeps its "exactly one
    state label" invariant. `add` rides in the same `add_labels` call so a
    counter (`fleet:fix-pass-N`) cannot land without the state it belongs to.

    Add first, then remove the others -- the order `expand._to_state` already
    uses. Reversed, an interruption between the two calls leaves the task with
    no state label, invisible to every ledger read: run 2026-09-25T11-23-12Z on
    project-b lost #22 that way when the daemon retired its integrator
    mid-command. Added first, the worst case is two state labels, which
    `ledger.read_one` refuses loudly.
    """
    new = labels.state_label(state)
    gh.add_labels(issue, [new, *(add or [])])
    gh.remove_labels(issue, [name for name in labels.all_state_labels() if name != new])


def _post(cfg: FleetConfig, gh: GhClient, packet: dict, state: str) -> dict:
    issue = packet["task"]
    # The issue outlives the attempt, so a packet that does not name its attempt
    # cannot be told apart from a previous one by anything reading the comments.
    # Read off the ledger row, not an argument: an agent that could set this
    # itself could re-validate a stale packet onto a fresh attempt -- exactly
    # the move Task 7's binding in decide._advance exists to prevent. `_post`
    # is reached only through build_handoff's callers (do_handoff, do_block,
    # do_escalate), so this never touches a verdict.
    packet["attempt"] = max(read_one(gh.get_issue(issue)).attempts, 1)
    validate(cfg, "handoff", packet)
    gh.comment(issue, render(packet))
    _move_state(gh, issue, state)
    return packet


def do_handoff(cfg: FleetConfig, gh: GhClient, *, task: int, to: str, role: str, **kw) -> dict:
    if to in {"blocked", "human"}:
        # build_handoff would reject these anyway -- it requires blocked_on for
        # BLOCKED and question+considered for HUMAN -- but it can only say which
        # field is missing. Name the command instead: the caller's mistake is not
        # a forgotten argument, it is the wrong entry point.
        #
        # Checked before the role gate deliberately: every role may block or
        # escalate, and "use do_block" is the more actionable message.
        verb = "do_block" if to == "blocked" else "do_escalate"
        raise ValueError(f"use {verb} to reach {to!r}, not a plain handoff")
    allowed = ROLE_MAY_HAND_TO.get(role)
    if allowed is None:
        # Not a default-allow: a typo in FLEET_ROLE would otherwise be a
        # privilege escalation rather than an error.
        raise ValueError(f"unknown role {role!r}; cannot check what it may hand to")
    if to not in allowed:
        permitted = ", ".join(sorted(allowed)) or "nothing"
        hint = " -- use `fleet verdict`" if role in VERDICT_ROLES else ""
        raise ValueError(f"role {role!r} may not hand to {to!r}; it may hand to {permitted}{hint}")
    gh.get_issue(task)
    return _post(cfg, gh, build_handoff(task=task, to=to, role=role, **kw), to)


def do_block(cfg: FleetConfig, gh: GhClient, *, task: int, on: int, **kw) -> dict:
    gh.get_issue(task)
    gh.get_issue(on)
    packet = build_handoff(task=task, to="blocked", blocked_on=on, **kw)
    return _post(cfg, gh, packet, "blocked")


def do_escalate(
    cfg: FleetConfig, gh: GhClient, *, task: int, question: str, considered: str, **kw
) -> dict:
    if not question.strip():
        raise ValueError("escalate needs a specific question, not an empty one")
    if not considered.strip():
        raise ValueError("escalate needs the options already considered")
    gh.get_issue(task)
    packet = build_handoff(
        task=task, to="human", question=question.strip(), considered=considered.strip(), **kw
    )
    return _post(cfg, gh, packet, "human")


def _verdict_target(
    verdict: str, fix_passes: int, max_fix_passes: int, answers: int = 0
) -> tuple[str, list[str]]:
    """Where a verdict sends a task, and which counter label rides along.

    Spec §5 ruling 4: approved -> INTEGRATING; `changes_required` -> another
    implementer pass with fix passes += 1, or NEEDS_HUMAN once the cap is spent.
    The counter IS the label -- `decide()` reads `fleet:fix-pass-N` back off the
    ledger, so a round that does not write one is a round the allocator cannot
    see, and an unattended run then cycles review -> ready -> review until the
    stall timer or the budget stops it.

    The fix-pass target is READY, not CLAIMED, though the spec's prose says
    "back to CLAIMED". CLAIMED means an agent holds the task, and nothing starts
    one at this moment; REAP reads a CLAIMED task with no live agent as ledger
    drift and releases it, which spends an attempt. Spec §5 ruling 2 says a fix
    pass costs no attempt, so CLAIMED here would break the rule this function
    exists to enforce. READY leaves DISPATCH to pick it up with `attempt`
    unchanged and leaves no window where a task is claimed by nobody.

    The cap is `max_fix_passes * (1 + answers)`, the fix-pass third of
    `model.effective_caps`: an owner's answer buys another allowance.
    """
    if verdict == "approved":
        return "integrating", []
    if fix_passes >= max_fix_passes * (1 + answers):
        return "human", []
    return "ready", [labels.fix_pass_label(fix_passes + 1)]


def do_verdict(
    cfg: FleetConfig,
    gh: GhClient,
    *,
    task: int,
    verdict: str,
    head_sha: str,
    reviewer: str,
    role: str,
    findings: list[dict],
    checks_rerun: str | None = None,
    packet_was_sufficient: bool | None = None,
) -> tuple[dict, str]:
    """Record a review verdict and move the task. REVIEW's only legal exit.

    Returns the payload and the state the task landed in: `changes_required`
    reaches two different states depending on the fix-pass counter, and the
    reviewer is owed the answer.
    """
    if role not in VERDICT_ROLES:
        # Without this an implementer approves its own task and reaches
        # INTEGRATING having had nobody read the diff -- the grading-integrity
        # hole ROLE_MAY_HAND_TO closes for handoffs, arriving by the other door.
        raise ValueError(
            f"role {role!r} may not write a verdict; only {', '.join(sorted(VERDICT_ROLES))} may"
        )
    if verdict == "changes_required" and not findings:
        # The schema allows an empty findings array, correctly -- an approval has
        # nothing to list. But "changes required" with nothing to change is a
        # task the implementer cannot action, and it spends the one bounded fix
        # pass. Refuse it here, where the state is known.
        raise ValueError("a changes_required verdict needs at least one finding")
    # read_one, not a local label scan: the write path and `decide()` then read
    # the same vocabulary through the same parser, so they cannot disagree about
    # what state a task is in or how many fix passes it has spent. It also
    # raises GhError for an issue that does not exist.
    current = read_one(gh.get_issue(task))
    if current.state != "review":
        # A verdict is REVIEW's exit, not a general relabelling tool. Without
        # this, `fleet verdict --verdict approved` on a task still CLAIMED walks
        # it straight past review into INTEGRATING.
        raise ValueError(
            f"task {task} is in {current.state!r}, not review; a verdict only exits REVIEW"
        )
    payload: dict = {
        "schema": "fleet.verdict.v1",
        "task": task,
        "verdict": verdict,
        "head_sha": head_sha,
        "reviewer": reviewer,
        "findings": findings,
    }
    if checks_rerun is not None:
        payload["checks_rerun"] = checks_rerun
    if packet_was_sufficient is not None:
        payload["packet_was_sufficient"] = packet_was_sufficient
    validate(cfg, "verdict", payload)
    state, extra = _verdict_target(
        verdict, current.fix_passes, cfg.max_fix_passes, answers=current.answers
    )
    # A fix pass past `max_fix_passes` exists only on an answered task, and its
    # label is outside the seeded vocabulary: gh refuses an unknown label by
    # failing the whole edit, so create it first. `_move_state` writes it
    # directly, not through HerdrExecutor.apply, which ensures its own.
    seeded = {labels.fix_pass_label(n) for n in range(1, cfg.max_fix_passes + 1)}
    for label in extra:
        if label not in seeded:
            gh.ensure_label(label)
    gh.comment(task, render(payload))
    _move_state(gh, task, state, extra)
    return payload, state


class DuplicateDiscovery(Exception):
    """An open issue with this title already exists."""


DISCOVERY_BODY = """Found while working #{parent}. Filed by an agent, not triaged.

## Evidence

{evidence}

## Acceptance criteria

_To be written by a scout during triage._
"""


#: The classes `fleet discover` files: what an agent finds (`bug`) and what a
#: planner splits an epic into (`patch`, `build`).
DISCOVER_CLASSES = ("patch", "build", "bug")

# What ChainExecutor's `Spec: (\S+)` reads back: one path, no whitespace.
_SPEC_PATH = re.compile(r"\S+")


def do_discover(
    cfg: FleetConfig,
    gh: GhClient,
    *,
    parent: int,
    title: str,
    area: str,
    evidence: str,
    klass: str = "bug",
    spec: str | None = None,
    policy: Policy | None = None,
) -> int:
    """File a discovery. A `bug` or `build` whose class the target routes to
    chain is filed READY and auto-ok, to run unattended; anything else --
    a `patch` above all, or a class not routed to chain -- lands in TRIAGE for
    a human or scout, as every discovery used to.

    A `build` names its spec, which must already be committed on
    `origin/<integration_branch>` because that is the branch chain builds
    from; its body's first line is `Spec: <path>`, the line ChainExecutor
    reads. `spec` is refused for every other class.

    `policy` is loaded from `cfg` when not given."""
    if klass not in DISCOVER_CLASSES:
        raise ValueError(f"discover files class {', '.join(DISCOVER_CLASSES)}, not {klass!r}")
    if klass == "build":
        if spec is None or not _SPEC_PATH.fullmatch(spec):
            raise ValueError("a build discovery needs --spec, one path with no spaces")
    elif spec is not None:
        raise ValueError(f"--spec is only for a build; a {klass} has no spec to build")
    # Strip first: search_titles is a substring scan over UNSTRIPPED stored
    # titles, so " foo" would not match a stored "foo" and the exact-match guard
    # below would never run.
    title = title.strip()
    if not title:
        # An empty title also defeats the duplicate guard below: search_titles("")
        # matches every issue, while the exact-match test matches none, so the
        # scan always passes and every blank discovery files another untitled
        # issue. do_escalate guards its own empty fields the same way.
        raise ValueError("discover needs a title, not an empty one")
    known = policy_module.area_names(cfg)
    if area not in known:
        raise ValueError(
            f"{area!r} is not an area in {cfg.ownership}; choose from {', '.join(sorted(known))}"
        )
    area_label = labels.area_label(area)
    if spec is not None:
        ref = f"origin/{cfg.integration_branch}"
        if not git_module.exists_on(cfg.root, ref, spec):
            raise ValueError(
                f"spec {spec!r} is not committed on {ref}; chain builds from there, "
                "so commit and push it first"
            )
    if policy is None:
        policy = policy_module.load(cfg)
    gh.get_issue(parent)
    # search_titles is a substring scan, so a short title collides with unrelated
    # issues -- "t" matches "...explicit k". Narrow with it, then require an exact
    # title match. The design already accepts that some near-duplicates get filed;
    # silently refusing a real discovery is the worse failure.
    #
    # OPEN only, though search_titles ("most recent 200, all states" -- other
    # callers rely on that contract, so the filter lives here instead) scans
    # every state: a closed discovery from a prior graded run must not block
    # re-filing it after a reset, or every run past the first exits 1 on a
    # rediscovery for a reason that has nothing to do with the orchestrator.
    # Two OPEN issues for one defect is the duplication this guard exists to
    # prevent; a closed one and a fresh one are not that. Case-folded through
    # .upper() for the same reason FakeGh.list_issues is: gh returns
    # OPEN/CLOSED and hand-built rows use open/closed.
    if any(
        i["title"].strip().lower() == title.strip().lower()
        and str(i.get("state", "OPEN")).upper() == "OPEN"
        for i in gh.search_titles(title)
    ):
        raise DuplicateDiscovery(f"an issue titled {title!r} already exists")
    # `discovered-by:<n>` is generated at runtime, so it does not exist in the
    # repo's label set. gh refuses an unknown label and creates no issue at all.
    parentage = f"discovered-by:{parent}"
    gh.ensure_label(parentage)
    body = DISCOVERY_BODY.format(parent=parent, evidence=evidence)
    if spec is not None:
        body = f"Spec: {spec}\n\n{body}"
    class_label = labels.class_label(klass)
    if klass != "patch" and chain_route_enabled(policy, klass):
        names = [labels.state_label("ready"), class_label, labels.AUTO_OK, area_label, parentage]
    else:
        names = [labels.state_label("triage"), area_label, parentage, class_label]
    return gh.create_issue(title, body, names)
