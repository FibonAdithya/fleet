from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import FIXTURE_POLICY
from fleet.model import Advance, CloseIssue, Continue, Dispatch, Rebase, Release, Retire
from fleet.runlog import RunLog
from fleet.runlog import read as read_runlog
from fleet.simulate import ScenarioError, _apply_to_rows, load_scenario, render_intent, run

ROOT = Path(__file__).resolve().parents[1]

CAPTURED = "fixtures/ledger/issues.json"

INLINE = {
    "name": "one-dispatch",
    "proves": "smoke",
    "seed": {
        "issues": [
            {
                "number": 1,
                "title": "t",
                "state": "OPEN",
                "labels": ["fleet:ready", "fleet:auto-ok", "area:eval", "class:patch"],
                "assignees": [],
            }
        ],
        "policy": str(FIXTURE_POLICY),
    },
    "ticks": [{"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]}],
}


def test_an_inline_scenario_dispatches():
    ticks = run(INLINE, ROOT)
    assert Dispatch(task=1, role="implementer", base="integration", attempt=1) in ticks[0]


def test_a_seeded_agent_is_visible_to_the_very_first_decide_call():
    """`seed.agents` exists to give decide() an agent it never started itself
    -- proof that seeding actually reaches the first tick, not merely that
    the schema accepts the key. #1 is READY and its seeded agent is
    "working": decide() cannot see it as a healthy continuation of anything
    (READY is not a live state), so the only way it is accounted for at all
    is Task 4's orphan pass -- and `_dispatch` also treats the task as busy,
    so no Dispatch competes with the Retire. `cost_usd` is left out of the
    seed on purpose: `AgentState.cost_usd` already defaults to 0.0, so this
    also proves the harness does not need every field to seed successfully."""
    scenario = {
        **INLINE,
        "seed": {**INLINE["seed"], "agents": {1: {"status": "working", "change_seq": 1}}},
        "ticks": [{"outcomes": {}, "expect": ["Retire(1)"]}],
    }
    assert run(scenario, ROOT)[0] == [Retire(task=1)]


def test_omitting_the_agents_seed_key_is_identical_to_an_explicit_empty_one():
    """Protects the other seven scenarios, none of which name `agents:` at
    all. `_numbered(None)` and `_numbered({})` both yield `{}`, and
    `FakeExecutor.seed` on an empty iterable is a no-op -- so the two must
    produce the exact same intents, not merely "similar" ones."""
    omitted = run(INLINE, ROOT)
    explicit = run({**INLINE, "seed": {**INLINE["seed"], "agents": {}}}, ROOT)
    want = [[Dispatch(task=1, role="implementer", base="integration", attempt=1)]]
    assert omitted == explicit == want


def test_a_seeded_agent_with_an_unknown_key_is_refused():
    """As strict as every other seed shape in this module: an unknown field
    on a seeded agent is a typo nobody would otherwise notice."""
    bad = {
        **INLINE,
        "seed": {
            **INLINE["seed"],
            "agents": {1: {"status": "working", "change_seq": 1, "role": "implementer"}},
        },
    }
    with pytest.raises(ScenarioError, match="unknown key"):
        run(bad, ROOT)


def test_a_seeded_agent_missing_change_seq_is_refused():
    """`AgentState.change_seq` has no default -- a scenario that omits it is
    asking the harness to invent a fact about a herdr snapshot, which is
    exactly the "quiet stub" the schema exists to refuse."""
    bad = {**INLINE, "seed": {**INLINE["seed"], "agents": {1: {"status": "working"}}}}
    with pytest.raises(ScenarioError, match="missing"):
        run(bad, ROOT)


def test_a_seeded_agent_with_an_unknown_status_is_refused():
    """`AgentState.status` is `"working" | "done" | "dirty"` by comment only
    (model.py) -- a fourth value here is a status no real herdr snapshot can
    report, and a scenario asserting on it would be asserting on a fake more
    permissive than the real client, which the global constraints forbid."""
    bad = {
        **INLINE,
        "seed": {**INLINE["seed"], "agents": {1: {"status": "sleeping", "change_seq": 1}}},
    }
    with pytest.raises(ScenarioError, match="unknown status"):
        run(bad, ROOT)


def test_expectations_are_checked_not_merely_recorded():
    """A harness that records without asserting is a harness that always passes."""
    wrong = {**INLINE, "ticks": [{"outcomes": {}, "expect": ["Dispatch(99, implementer, 1)"]}]}
    with pytest.raises(AssertionError, match="tick 1"):
        run(wrong, ROOT)


def test_an_expectation_that_omits_the_attempt_number_does_not_match():
    """The attempt number is half of every stall and poison-task claim. If the
    renderer dropped it, `Release(1, stalled)` would match a release on any
    attempt at all, including the third one that must never happen."""
    stale = {**INLINE, "ticks": [{"outcomes": {}, "expect": ["Dispatch(1, implementer)"]}]}
    with pytest.raises(AssertionError, match="tick 1"):
        run(stale, ROOT)


def test_an_unquoted_expect_entry_is_refused_rather_than_silently_unmatched():
    """pyyaml reads flow-style `[Dispatch(1, implementer, 1)]` as three separate
    strings, none of which can ever equal a rendered intent. Compared as strings
    that is a scenario which fails for the wrong reason -- or, on a tick whose
    other entries happen to match, one that passes having asserted garbage."""
    text = (
        "name: n\n"
        "proves: p\n"
        "seed:\n"
        "  issues: []\n"
        "  policy: " + str(FIXTURE_POLICY) + "\n"
        "ticks:\n"
        "  - outcomes: {}\n"
        "    expect: [Dispatch(1, implementer, 1)]\n"
    )
    with pytest.raises(ScenarioError, match="cannot parse"):
        run(yaml.safe_load(text), ROOT)


def test_an_expectation_naming_something_that_is_not_an_intent_is_refused():
    typo = {**INLINE, "ticks": [{"outcomes": {}, "expect": ["Dispath(1, implementer, 1)"]}]}
    with pytest.raises(ScenarioError, match="not an intent"):
        run(typo, ROOT)


def test_an_empty_expect_asserts_that_nothing_happened():
    """The poison-task scenario's final tick asserts an absence. If `expect: []`
    meant "do not check", the claim it proves would be unproven."""
    quiet = {
        **INLINE,
        "seed": {"issues": [], "policy": str(FIXTURE_POLICY)},
        "ticks": [{"outcomes": {}, "expect": []}],
    }
    run(quiet, ROOT)

    noisy = {**INLINE, "ticks": [{"outcomes": {}, "expect": []}]}
    with pytest.raises(AssertionError, match="tick 1"):
        run(noisy, ROOT)


def test_a_captured_ledger_seed_dispatches_every_free_area_at_once():
    """The captured ledger is the default seed, and this is the shape it has:
    eleven READY tasks across six areas, and both CLOSED rows -- #13 in triage
    and #12 still labelled fleet:review and area:docs -- hold neither a slot nor
    an area, so all six slots fill. #9 is the capture's only class:spec row and
    therefore the only planner route it can produce. A comparison that listed
    one Dispatch would be a comparison that is not exhaustive."""
    scenario = {
        "name": "captured",
        "proves": "real shapes",
        "seed": {"ledger": CAPTURED, "policy": str(FIXTURE_POLICY)},
        "ticks": [
            {
                "outcomes": {},
                "expect": [
                    "Dispatch(1, implementer, 1)",
                    "Dispatch(2, implementer, 1)",
                    "Dispatch(4, implementer, 1)",
                    "Dispatch(5, implementer, 1)",
                    "Dispatch(6, implementer, 1)",
                    "Dispatch(9, planner, 1)",
                ],
            }
        ],
    }
    assert len(run(scenario, ROOT)[0]) == 6


def test_a_repo_relative_policy_path_is_joined_to_root(monkeypatch, tmp_path):
    """Every scenario file writes `policy: fixtures/policy/fleet-fixture`.
    Unjoined, that is resolved against the process CWD and `_find_root` walks
    UP from there -- so running pytest from anywhere else silently loads some
    other repo's fleet.toml, or none, and the scenario proves a claim about a
    policy it never read."""
    monkeypatch.chdir(tmp_path)
    scenario = {
        **INLINE,
        "seed": {**INLINE["seed"], "policy": "fixtures/policy/fleet-fixture"},
    }
    run(scenario, ROOT)


def test_time_advances_by_one_stall_threshold_per_tick():
    """A stall takes stall_minutes of simulated time and no wall-clock at all,
    and it takes two frozen ticks: a first-seen agent has idle == 0."""
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            {"outcomes": {1: "stall"}, "expect": []},
            {"outcomes": {1: "stall"}, "expect": ["Release(1, stalled, 2)"]},
        ],
    }
    ticks = run(scenario, ROOT)
    assert Release(task=1, reason="stalled", attempt=2) in ticks[2]


def test_a_scripted_outcome_lasts_one_tick_only():
    """A tick's `outcomes:` is the complete scripting for that tick. Sticky
    scripting made spec §7's `outcomes: {}  # nothing running yet` mean "keep
    yesterday's failure", so a scenario could never show an agent recovering --
    and the tick that looks like a reset is the one every reader trusts."""
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            {"outcomes": {1: "stall"}, "expect": []},
            {"outcomes": {}, "expect": []},
            {"outcomes": {}, "expect": []},
        ],
    }
    run(scenario, ROOT)


def test_a_clean_exit_advances_the_task_to_its_packets_target():
    """The happy path. Without a packet on the outcome, `clean_exit` produces a
    done agent whose task has nothing to advance to, and no scenario in the set
    can move a task past `claimed`."""
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            {
                "outcomes": {1: "clean_exit"},
                "packets": {1: {"to": "review", "attempt": 1, "head_sha": "a91c204"}},
                "expect": ["Advance(1, review)"],
            },
        ],
    }
    assert Advance(task=1, to="review") in run(scenario, ROOT)[1]


def test_a_seeded_packet_supplies_the_dependency_an_unblock_needs():
    """blocked_on is read off the packet, never a label -- there is no
    fleet:blocked-on-N. With no packets seeded, `blocked_on` is None for every
    task and the whole UNBLOCK phase is unreachable."""
    scenario = {
        "name": "blocked",
        "proves": "unblock needs a packet",
        "seed": {
            "policy": str(FIXTURE_POLICY),
            "packets": {4: {"to": "blocked", "blocked_on": 11}},
            "issues": [
                {
                    "number": 4,
                    "title": "t",
                    "state": "OPEN",
                    "labels": ["fleet:blocked", "fleet:auto-ok", "area:models", "class:patch"],
                    "assignees": [],
                },
                {
                    "number": 11,
                    "title": "t",
                    "state": "OPEN",
                    "labels": ["fleet:done", "fleet:auto-ok", "area:data", "class:patch"],
                    "assignees": [],
                },
            ],
        },
        "ticks": [{"outcomes": {}, "expect": ["Unblock(4)"]}],
    }
    run(scenario, ROOT)


def test_the_run_log_records_every_intent_of_every_tick(tmp_path):
    """Spec §5 requires the log and the fold already returns exactly what it
    records. Wired to nothing, it ships dark and rubric 04 still cannot answer
    "was fleet:blocked ever applied"."""
    log = RunLog(tmp_path / "run.jsonl", run_id="sim")
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            {"outcomes": {1: "dirty_exit"}, "expect": ["Release(1, dirty_exit, 2)"]},
        ],
    }
    run(scenario, ROOT, runlog=log)
    entries = read_runlog(tmp_path / "run.jsonl")
    assert [(e["tick"], e["intent"]["kind"]) for e in entries] == [(1, "Dispatch"), (2, "Release")]


def test_the_rows_carry_the_last_ticks_labels_into_the_next():
    """The attempt counter lives in a label, so a fold that does not write the
    label back re-dispatches attempt 1 forever and the cap never binds."""
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            {"outcomes": {1: "dirty_exit"}, "expect": ["Release(1, dirty_exit, 2)"]},
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 2)"]},
        ],
    }
    run(scenario, ROOT)


def test_apply_to_rows_closes_the_row_on_close_issue():
    """CloseIssue is an effect, not merely a label move -- a fold that only
    watches SetLabels leaves the row's `state` at OPEN forever. That `state` is
    what `ledger.read_one` turns into `is_open`, the field `_reap`'s own
    comment names 'our own Advance-to-done on a previous tick' as one of the
    two ways a row goes stale under -- and it is what makes Task 22 able to
    seed a scenario that carries a closed row from one tick into the next."""
    rows = [{"number": 1, "title": "t", "state": "OPEN", "labels": [], "assignees": []}]
    updated = _apply_to_rows(rows, [CloseIssue(task=1)])
    assert updated[0]["state"] == "CLOSED"


def test_apply_to_rows_leaves_other_rows_open_on_close_issue():
    rows = [
        {"number": 1, "title": "t", "state": "OPEN", "labels": [], "assignees": []},
        {"number": 2, "title": "t", "state": "OPEN", "labels": [], "assignees": []},
    ]
    updated = _apply_to_rows(rows, [CloseIssue(task=1)])
    assert updated[0]["state"] == "CLOSED"
    assert updated[1]["state"] == "OPEN"


def test_an_unknown_scenario_key_is_refused():
    with pytest.raises(ScenarioError, match="unknown key"):
        run({**INLINE, "tickz": []}, ROOT)


def test_an_unknown_tick_key_is_refused():
    """`expects:` for `expect:` read as "do not check", so the file passed while
    proving nothing -- the exact vacuity `expect: []` was designed against, one
    level down."""
    with pytest.raises(ScenarioError, match="tick 1"):
        run({**INLINE, "ticks": [{"outcomes": {}, "expects": []}]}, ROOT)


def test_a_tick_with_no_expectation_is_refused():
    with pytest.raises(ScenarioError, match="no expect"):
        run({**INLINE, "ticks": [{"outcomes": {}}]}, ROOT)


def test_a_scenario_without_ticks_is_refused():
    with pytest.raises(ScenarioError, match="no ticks"):
        run({"name": "x", "proves": "y", "seed": {"issues": [], "policy": "."}}, ROOT)


def test_a_missing_seed_is_a_scenario_error_not_a_key_error():
    """`ScenarioError` says "this file could not be understood". A bare KeyError
    says a traceback, and the one on `name` fires inside the failure message of
    the assertion it was about to report -- destroying the report with a bug in
    the reporter."""
    with pytest.raises(ScenarioError, match="no seed"):
        run({"name": "x", "proves": "y", "ticks": [{"outcomes": {}, "expect": []}]}, ROOT)


def test_a_missing_name_is_a_scenario_error_not_a_key_error():
    """`scenario["name"]` appears unguarded in the AssertionError message `run`
    raises on a mismatch. A bare KeyError on a missing `name` would fire from
    inside that formatting instead of from validation -- the reporter itself
    breaking, and breaking silently for every scenario nobody wrote a `name`
    into."""
    no_name = {k: v for k, v in INLINE.items() if k != "name"}
    with pytest.raises(ScenarioError, match="no name"):
        run(no_name, ROOT)


def test_a_missing_seed_policy_is_a_scenario_error_not_a_key_error():
    """`seed["policy"]` is read unguarded to join against `root` and load
    fleet.toml. Without this check a missing policy fails as a bare KeyError
    (or worse, resolves against the process CWD) instead of the ScenarioError
    the schema's whole contract promises."""
    no_policy = {**INLINE, "seed": {k: v for k, v in INLINE["seed"].items() if k != "policy"}}
    with pytest.raises(ScenarioError, match="no policy"):
        run(no_policy, ROOT)


def test_a_seed_naming_both_a_ledger_and_inline_issues_is_refused():
    """One silently won. A scenario whose hand-written rows are ignored proves
    something about a ledger its author never looked at."""
    both = {**INLINE, "seed": {**INLINE["seed"], "ledger": CAPTURED}}
    with pytest.raises(ScenarioError, match="both"):
        run(both, ROOT)


def test_render_intent_drops_base_and_nothing_else():
    intent = Dispatch(task=5, role="implementer", base="integration", attempt=2)
    assert render_intent(intent) == "Dispatch(5, implementer, 2)"


def test_a_rendered_retire_is_a_recognized_intent_shape():
    """decide() does not emit Retire yet -- Task 3 does -- but a scenario's
    `expect:` list is checked against `_shape`, which refuses any name absent
    from `_INTENTS`. Without Retire in that list, the first scenario Task 3
    writes to prove it dies with `'Retire' is not an intent` before its
    assertion is even compared."""
    from fleet.simulate import _shape

    assert _shape(render_intent(Retire(task=5))) == ("Retire", ("5",))


def test_a_rendered_continue_is_a_recognized_intent_shape():
    """A continuation is an intent a scenario must be able to `expect:`, and
    `_shape` refuses any name absent from `_INTENTS`. Without Continue in that
    list every scenario that carries a task past REVIEW dies with `'Continue' is
    not an intent` before its assertion is compared -- which is the whole of M3's
    vertical slice."""
    from fleet.simulate import _shape

    rendered = render_intent(Continue(task=5, role="reviewer", base="integration"))
    assert rendered == "Continue(5, reviewer)"
    assert _shape(rendered) == ("Continue", ("5", "reviewer"))


def test_a_rendered_rebase_is_a_recognized_intent_shape():
    """decide() emits Rebase for an integrator's handback (Task 6), and Task
    20's rebase-storm scenario asserts one with `expect:`. `_shape` refuses any
    name absent from `_INTENTS`; without Rebase in that list the scenario dies
    with `'Rebase' is not an intent` before its assertion is even compared."""
    from fleet.simulate import _shape

    rendered = render_intent(Rebase(task=4, rebase=1))
    assert rendered == "Rebase(4, 1)"
    assert _shape(rendered) == ("Rebase", ("4", "1"))


def test_load_scenario_reads_yaml(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(INLINE))
    assert load_scenario(path)["name"] == "one-dispatch"


# --------------------------------------------------- the agent moves the label


def test_a_tick_can_relabel_a_row_the_way_an_agent_does():
    """The shell's ordinary path, and until now inexpressible here.

    An agent ends a stage by running `fleet handoff --to <state>`, and
    `commands._post` moves the label itself -- so the allocator's next tick
    reads a row that has already moved. `_apply_to_rows` folds only the
    allocator's OWN effects, so a scenario could previously describe a label
    move only by having decide() make it, which is the shape the real shell
    rarely takes. #1 starts READY and is dispatched; its agent then moves the
    label to REVIEW itself and goes, so tick 2 reads a REVIEW row with no agent
    and continues it to the reviewer. Without the relabel the row is still
    CLAIMED and tick 2 releases it as ledger drift instead.
    """
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            {
                "relabel": {1: "review"},
                "outcomes": {1: "vanished"},
                "expect": ["Continue(1, reviewer)"],
            },
        ],
    }
    assert run(scenario, ROOT)[1] == [Continue(task=1, role="reviewer", base="integration")]


def test_a_relabel_replaces_the_old_state_label_rather_than_adding_to_it():
    """Two state labels on one row is a ledger `ledger.read_one` cannot read
    honestly, and it is what a relabel that only appends would produce. Written
    exactly as `expand._to_state` writes one: every other state label off."""
    scenario = {
        **INLINE,
        "ticks": [
            {"outcomes": {}, "expect": ["Dispatch(1, implementer, 1)"]},
            # If `fleet:ready` survived the move, #1 would still look
            # dispatchable and tick 2 would try to start a second agent on it.
            {"relabel": {1: "human"}, "outcomes": {}, "expect": ["Retire(1)"]},
        ],
    }
    assert run(scenario, ROOT)[1] == [Retire(task=1)]


def test_a_relabel_to_a_state_with_no_label_is_refused():
    """As strict as every other shape in this schema. A typo would move a row
    to a state nothing reads, and the tick would then assert an absence it
    produced by accident rather than by policy."""
    bad = {
        **INLINE,
        "ticks": [{"relabel": {1: "finished"}, "outcomes": {}, "expect": []}],
    }
    with pytest.raises(ScenarioError, match="unknown state"):
        run(bad, ROOT)
