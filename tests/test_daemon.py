from __future__ import annotations

import signal
import threading
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FIXTURE_POLICY
from fleet import policy as policy_module
from fleet import runlog as runlog_module
from fleet.config import load_config
from fleet.daemon import Daemon, stop_on_signals
from fleet.gh import FakeGh, GhError
from fleet.herdr import FakeHerdr, HerdrError
from fleet.herdr_executor import HerdrExecutor
from fleet.model import Release
from fleet.runlog import RunLog, spent_from_log

T0 = datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc)

#: FakeGh mirrors `gh`: an assignee it cannot resolve fails the whole edit. Its
#: known-login set is `{"FibonAdithya"}`, so a daemon test that writes ownership
#: has to use that name -- an invented one raises GhError out of
#: `HerdrExecutor._start`, which catches HerdrError only, and takes the tick with
#: it. (The brief's `owner="tester"` does exactly that.)
OWNER = "FibonAdithya"

READY = ["fleet:ready", "area:eval", "class:patch", "fleet:auto-ok"]
CLAIMED = ["fleet:claimed", "fleet:attempt-1", "area:eval", "class:patch", "fleet:auto-ok"]


@pytest.fixture()
def cfg():
    """The vendored fixture policy layer, read-only.

    conftest's `cfg` builds a tmp repo with schemas and no policy tables, so
    `policy.load` cannot read it and `build_prompt` has no role prompt to open --
    and every dispatch this module makes goes through both.
    """
    return load_config(FIXTURE_POLICY)


def _daemon(cfg, tmp_path, gh=None, herdr=None, run_id="r1"):
    gh = gh if gh is not None else FakeGh()
    herdr = herdr if herdr is not None else FakeHerdr()
    policy = policy_module.load(cfg)
    log = RunLog(tmp_path / "run.jsonl", run_id=run_id)
    executor = HerdrExecutor(herdr, gh, cfg, owner=OWNER)
    return Daemon(cfg, policy, gh, executor, log, run_id=run_id), gh, herdr


def _kinds(tmp_path):
    return [e["intent"]["kind"] for e in runlog_module.read(tmp_path / "run.jsonl")]


class _BrokenGh:
    """A gh whose list call fails, wrapping a real fake for everything else.

    A GitHub outage is the reachable way a whole tick raises: `read_ledger` is
    the first call the tick makes and `GhError` is not `HerdrError`, so nothing
    below the daemon catches it.
    """

    def __init__(self, inner: FakeGh, stop: threading.Event | None = None, after: int = 0) -> None:
        self.inner = inner
        self.calls = 0
        self.stop = stop
        self.after = after

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def list_issues(self, state):
        self.calls += 1
        if self.stop is not None and self.calls >= self.after:
            self.stop.set()
        raise GhError("gh issue list failed: HTTP 503")


class _LateHerdr(FakeHerdr):
    """Learns which models it cannot price only by reading the snapshot.

    `RealHerdr.unpriced` reports what the last `agents()` call could not price,
    and `FakeHerdr`'s seeded set is true before any read -- which hides the
    ordering entirely. This one does not.
    """

    def agents(self):
        self.unpriced_models = {"no price for model 'claude-new'"}
        return super().agents()


class _StubbornHerdr(FakeHerdr):
    """Every stop fails with a code that is not `agent_not_found`.

    The executor's cautious path: the agent may still be running, so the
    worktree is left alone and the failure is recorded rather than raised.
    """

    def stop_agent(self, task: int) -> None:
        raise HerdrError(f"herdr agent send-keys fleet-{task} failed", code="invalid_key")


# ---------------------------------------------------------------- the tick


def test_a_tick_dispatches_and_the_effects_land(cfg, tmp_path):
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    number = gh.create_issue("Fix LID", "b", READY)
    daemon.tick(T0)
    assert number in herdr.worktrees
    assert "fleet:claimed" in gh.get_issue(number)["labels"]
    assert gh.get_issue(number)["assignees"] == [OWNER]


def test_every_intent_reaches_the_run_log(cfg, tmp_path):
    """Harness §7 makes the log one of the three inputs `fleet grade` needs. An
    intent applied but unlogged is a transition the grader cannot see -- and the
    transient labels are the ones only the log can answer for."""
    daemon, gh, _ = _daemon(cfg, tmp_path)
    gh.create_issue("Fix LID", "b", READY)
    daemon.tick(T0)
    entries = runlog_module.read(tmp_path / "run.jsonl")
    assert [e["intent"]["kind"] for e in entries] == ["Dispatch"]
    assert entries[0]["tick"] == 1
    assert entries[0]["at"] == T0.isoformat()


def test_a_reaping_intent_logs_the_cost_it_finalised(cfg, tmp_path):
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    number = gh.create_issue("Fix LID", "b", CLAIMED)
    herdr.add_agent(task=number, status="dirty", change_seq=2, cost_usd=1.25)
    daemon.tick(T0)
    entries = runlog_module.read(tmp_path / "run.jsonl")
    assert entries[0]["cost_usd"] == pytest.approx(1.25)


def test_a_non_reaping_intent_carries_no_cost(cfg, tmp_path):
    """`AgentState.cost_usd` is cumulative per agent. Charging a live agent's
    running total every tick re-adds the same dollars, and six agents at $0.50
    drain a $25 fleet in seven ticks of a night that spent three."""
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    gh.create_issue("Fix LID", "b", READY)
    daemon.tick(T0)
    entries = runlog_module.read(tmp_path / "run.jsonl")
    assert entries[0]["intent"]["kind"] == "Dispatch"
    assert entries[0]["cost_usd"] == 0.0


def test_one_agent_is_charged_once_even_when_two_intents_reap_it(cfg, tmp_path):
    """A `spec` row at REVIEW with a finished agent draws two REAPING intents --
    Retire from `_reap`, Escalate(unroutable_stage) from `_unroutable` -- and
    `decide` charges the agent once, through a set. A log that charges per intent
    doubles the night's spend on restart and drains a fleet that spent half."""
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    number = gh.create_issue(
        "Write the spec",
        "b",
        ["fleet:review", "fleet:attempt-1", "area:eval", "class:spec", "fleet:auto-ok"],
    )
    herdr.add_agent(task=number, status="done", change_seq=3, cost_usd=1.25)
    daemon.tick(T0)
    entries = runlog_module.read(tmp_path / "run.jsonl")
    assert sorted(e["intent"]["kind"] for e in entries) == ["Escalate", "Retire"]
    assert sum(e["cost_usd"] for e in entries) == pytest.approx(1.25)


def test_the_log_and_the_live_state_agree_on_the_spend_every_tick(cfg, tmp_path):
    """The invariant restart recovery stands on. `spent_from_log` is not an
    approximation of `FleetState.spent_usd`; it is the same number, or a restart
    resumes with a different budget from the one it stopped with."""
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    first = gh.create_issue("Fix LID", "b", CLAIMED)
    second = gh.create_issue(
        "Write the spec",
        "b",
        ["fleet:review", "fleet:attempt-1", "area:models", "class:spec", "fleet:auto-ok"],
    )
    herdr.add_agent(task=first, status="dirty", change_seq=2, cost_usd=0.75)
    herdr.add_agent(task=second, status="done", change_seq=3, cost_usd=1.25)
    for index in range(3):
        daemon.tick(T0 + timedelta(minutes=index))
        assert spent_from_log(tmp_path / "run.jsonl", run_id="r1") == pytest.approx(
            daemon.state.spent_usd
        )
    assert daemon.state.spent_usd > 0.0


# ---------------------------------------------------------------- restart


def test_a_restarted_daemon_recovers_the_night_s_spend(cfg, tmp_path):
    """The half of the restart test that costs money, taken by the path a
    restart actually takes: one daemon spends, the process dies, a second
    daemon is constructed over the same log and run id and resumes with the
    same total. A daemon whose spent_usd resets hands the fleet a second full
    budget at 2am, and the drain never fires."""
    first, gh, herdr = _daemon(cfg, tmp_path, run_id="tonight")
    number = gh.create_issue("Fix LID", "b", CLAIMED)
    herdr.add_agent(task=number, status="dirty", change_seq=2, cost_usd=7.5)
    first.tick(T0)
    assert first.state.spent_usd == pytest.approx(7.5)

    # kill -9: nothing is handed over, and the second daemon shares only the
    # log file and the run id.
    second, _, _ = _daemon(cfg, tmp_path, gh=gh, herdr=herdr, run_id="tonight")
    assert second.state.spent_usd == pytest.approx(7.5)


def test_a_tick_that_dies_applying_still_records_what_it_committed(cfg, tmp_path):
    """The crash the restart has to survive lands mid-tick, not between ticks.

    The log is written and `self.state` advanced before the effects go out, so a
    tick that dies applying leaves the two agreeing and the restart resumes on
    the same ceiling. Reversed, the spend of the tick that crashed is refunded
    -- and a fleet that crashes once a night is handed the difference back every
    time.
    """
    daemon, gh, herdr = _daemon(cfg, tmp_path, run_id="tonight")
    number = gh.create_issue("Fix LID", "b", CLAIMED)
    herdr.add_agent(task=number, status="dirty", change_seq=2, cost_usd=7.5)

    def _die(effects):
        raise RuntimeError("the box went away mid-batch")

    daemon.executor.apply = _die
    with pytest.raises(RuntimeError):
        daemon.tick(T0)
    assert daemon.state.spent_usd == pytest.approx(7.5)

    restarted, _, _ = _daemon(cfg, tmp_path, gh=gh, herdr=herdr, run_id="tonight")
    assert restarted.state.spent_usd == pytest.approx(7.5)


def test_a_restarted_daemon_keeps_the_ceiling_binding(cfg, tmp_path):
    """The reason the number matters. Recovered spend past the drain fraction
    must stop the restarted daemon dispatching, or the recovery is a display
    value with no consequence."""
    log = RunLog(tmp_path / "run.jsonl", run_id="tonight")
    log.append(
        tick=1,
        at=T0,
        intent=Release(task=99, reason="stalled", attempt=2),
        outcome="applied",
        cost_usd=24.0,
    )
    daemon, gh, herdr = _daemon(cfg, tmp_path, run_id="tonight")
    number = gh.create_issue("Fix LID", "b", READY)
    snapshot = daemon.tick(T0)
    assert "Dispatch" not in _kinds(tmp_path)
    assert number not in herdr.worktrees
    assert snapshot.draining is True
    assert "budget" in snapshot.drain_reason


def test_a_fresh_run_id_starts_at_zero(cfg, tmp_path):
    """One log file holds several nights. Recovering all of them would start
    tonight already drained."""
    log = RunLog(tmp_path / "run.jsonl", run_id="old")
    log.append(
        tick=1,
        at=T0,
        intent=Release(task=1, reason="stalled", attempt=2),
        outcome="applied",
        cost_usd=9.0,
    )
    daemon, _, _ = _daemon(cfg, tmp_path, run_id="tonight")
    assert daemon.state.spent_usd == 0.0


# ---------------------------------------------------------------- unpriced


def test_an_unpriced_model_drains_without_stopping_the_reap(cfg, tmp_path):
    """Drain, do not kill. Dropping the dispatches stops new spend; keeping the
    reap means a stalled agent is still noticed while a human fixes the price
    table."""
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    gh.create_issue("Fix LID", "b", READY)
    reapable = gh.create_issue("Fix eval", "b", CLAIMED)
    herdr.add_agent(task=reapable, status="dirty", change_seq=2, cost_usd=0.5)
    herdr.unpriced_models = {"no price for model 'claude-new'"}
    snapshot = daemon.tick(T0)
    kinds = _kinds(tmp_path)
    assert "Dispatch" not in kinds
    # The reap is the half that must survive: a drain that also dropped it would
    # leave a dirty agent holding its slot and its area all night.
    assert "Release" in kinds or "Escalate" in kinds
    assert "Drain" in kinds
    assert snapshot.draining is True
    assert "claude-new" in snapshot.drain_reason


def test_an_unpriced_model_stops_continuations_too(cfg, tmp_path):
    """A Continue starts an agent exactly as a Dispatch does, so it is new spend
    on a model nothing can price. `_dispatch`'s own budget drain returns
    `[Drain]` and drops both passes; a drain here that dropped only Dispatch
    would let the whole review-and-integrate half of the fleet keep starting
    agents against a ceiling that has stopped measuring."""
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    number = gh.create_issue(
        "Fix LID",
        "b",
        ["fleet:review", "fleet:attempt-1", "area:eval", "class:patch", "fleet:auto-ok"],
    )
    herdr.unpriced_models = {"no price for model 'claude-new'"}
    daemon.tick(T0)
    assert "Continue" not in _kinds(tmp_path)
    assert number not in herdr.worktrees


def test_the_price_table_is_read_after_the_snapshot_not_before(cfg, tmp_path):
    """`unpriced()` answers for the last `agents()` read. Asked first it answers
    for the previous tick, so tick 1 never drains -- and tick 1 is the whole of
    a `fleet run --once`, and the tick that dispatches a night's backlog."""
    daemon, gh, _ = _daemon(cfg, tmp_path, herdr=_LateHerdr())
    number = gh.create_issue("Fix LID", "b", READY)
    snapshot = daemon.tick(T0)
    assert snapshot.draining is True
    assert "Dispatch" not in _kinds(tmp_path)
    assert number not in daemon.executor.herdr.worktrees


def test_an_unpriced_agent_is_never_charged_as_a_free_one(cfg, tmp_path):
    """`price_transcript` raises rather than pricing an unknown model at zero, so
    an unpriced agent must not reach the daemon as a $0.00 one either. It reaches
    it as a drain."""
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    number = gh.create_issue("Fix LID", "b", CLAIMED)
    herdr.add_agent(task=number, status="dirty", change_seq=2, cost_usd=0.0)
    herdr.unpriced_models = {"no price for model 'claude-new'"}
    snapshot = daemon.tick(T0)
    assert snapshot.draining is True
    assert snapshot.spent_usd == 0.0
    # The drain is the record that the zero is not a measurement.
    assert "unpriced" in snapshot.drain_reason


def test_both_drain_reasons_survive_when_both_fire(cfg, tmp_path):
    """Two independent reasons to stop spending are two facts. A reader that
    sees only "budget" fixes the wrong thing at 3am."""
    log = RunLog(tmp_path / "run.jsonl", run_id="tonight")
    log.append(
        tick=1,
        at=T0,
        intent=Release(task=99, reason="stalled", attempt=2),
        outcome="applied",
        cost_usd=24.0,
    )
    daemon, gh, herdr = _daemon(cfg, tmp_path, run_id="tonight")
    gh.create_issue("Fix LID", "b", READY)
    herdr.unpriced_models = {"no price for model 'claude-new'"}
    snapshot = daemon.tick(T0)
    assert "budget" in snapshot.drain_reason
    assert "claude-new" in snapshot.drain_reason


# ---------------------------------------------------------------- snapshot


def test_the_snapshot_reports_spend_slots_and_held_areas(cfg, tmp_path):
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    number = gh.create_issue("Fix LID", "b", CLAIMED)
    herdr.add_agent(task=number, status="working", change_seq=1, cost_usd=0.5)
    snapshot = daemon.tick(T0)
    assert snapshot.tick == 1
    assert "eval" in snapshot.held_areas
    assert snapshot.free_slots == 5
    assert any(row["task"] == number for row in snapshot.rows)


def test_the_snapshot_round_trips_through_json(cfg, tmp_path):
    """Task 16 serves `to_json()` over a socket. A tuple or a Path in there is a
    TypeError inside the socket thread, where nothing sees it."""
    import json

    daemon, gh, herdr = _daemon(cfg, tmp_path)
    gh.create_issue("Fix LID", "b", CLAIMED)
    herdr.add_agent(task=1, status="working", change_seq=1, cost_usd=0.5)
    payload = json.loads(json.dumps(daemon.tick(T0).to_json()))
    assert payload["tick"] == 1
    assert payload["held_areas"] == ["eval"]
    assert payload["run_id"] == "r1"


def test_two_ticks_advance_the_counter_and_carry_state(cfg, tmp_path):
    """decide() is a fold. A daemon that rebuilt FleetState each tick would
    restart every stall timer every minute, so no stall could ever be
    detected."""
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    number = gh.create_issue("Fix LID", "b", CLAIMED)
    herdr.add_agent(task=number, status="working", change_seq=1)
    daemon.tick(T0)
    second = daemon.tick(T0 + timedelta(seconds=60))
    assert second.tick == 2
    assert daemon.state.observation(number).since == T0


# ---------------------------------------------------------------- the loop


def test_run_stops_when_the_event_is_set(cfg, tmp_path):
    """The daemon must exit its loop on a signal rather than on a kill, or a
    graceful stop is indistinguishable from the crash the restart test needs to
    be able to tell apart."""
    daemon, _, _ = _daemon(cfg, tmp_path)
    stop = threading.Event()
    ticks = []
    daemon.tick = lambda now: (ticks.append(now), stop.set())[0]
    daemon.run(poll_seconds=0.01, stop=stop)
    assert len(ticks) == 1


def test_run_takes_its_time_from_the_clock_it_is_given(cfg, tmp_path):
    """Time is a value here as it is in the core. A loop that read the wall
    clock could only be tested by sleeping, and a test that sleeps to
    synchronise is a flake waiting for a loaded box."""
    daemon, gh, _ = _daemon(cfg, tmp_path)
    gh.create_issue("Fix LID", "b", READY)
    stop = threading.Event()
    published = []
    daemon.run(
        poll_seconds=0,
        stop=stop,
        publish=lambda snapshot: (published.append(snapshot), stop.set())[0],
        clock=lambda: T0,
    )
    assert [s.at for s in published] == [T0.isoformat()]


def test_a_failing_tick_does_not_stop_the_loop(cfg, tmp_path):
    """A GitHub outage lasts minutes; the night lasts hours. A tick that dies
    must cost one tick, not the run -- and the next tick reads the ledger
    again, which is the whole recovery."""
    stop = threading.Event()
    gh = _BrokenGh(FakeGh(), stop=stop, after=3)
    daemon, _, _ = _daemon(cfg, tmp_path, gh=gh)
    daemon.run(poll_seconds=0, stop=stop, clock=lambda: T0)
    assert gh.calls == 3
    assert len(daemon.failures) == 3
    assert "503" in daemon.failures[-1]


def test_a_failing_tick_leaves_the_last_good_snapshot_standing(cfg, tmp_path):
    """Stale by one tick is a fact a reader can weigh -- the snapshot carries
    its own tick number. `None` after a working night is not."""
    inner = FakeGh()
    inner.create_issue("Fix LID", "b", CLAIMED)
    daemon, _, herdr = _daemon(cfg, tmp_path, gh=inner)
    herdr.add_agent(task=1, status="working", change_seq=1, cost_usd=0.5)
    good = daemon.tick(T0)

    daemon.gh = _BrokenGh(inner)
    with pytest.raises(GhError):
        daemon.tick(T0 + timedelta(minutes=1))
    assert daemon.snapshot is good
    assert daemon.state.spent_usd == 0.0


def test_a_keyboard_interrupt_is_not_swallowed(cfg, tmp_path):
    """Ctrl-C is not a failing tick. A loop that caught BaseException would make
    the daemon unkillable from the terminal that started it."""
    daemon, _, _ = _daemon(cfg, tmp_path)
    stop = threading.Event()

    def _interrupt(now):
        # Set FIRST, so a loop that swallowed the interrupt would exit its
        # `while` cleanly and fail this test on the missing raise. Raising
        # without it, a swallowing loop spins forever and the test hangs --
        # which is a broken test, not a failing one.
        stop.set()
        raise KeyboardInterrupt

    daemon.tick = _interrupt
    with pytest.raises(KeyboardInterrupt):
        daemon.run(poll_seconds=0, stop=stop, clock=lambda: T0)


def test_the_failure_record_is_bounded(cfg, tmp_path):
    """Anything that accumulates per tick has to have a bound. A herdr or a gh
    that fails every tick appends once a tick, all night, into a list nothing
    ever drains."""
    stop = threading.Event()
    gh = _BrokenGh(FakeGh(), stop=stop, after=Daemon.FAILURE_LIMIT + 6)
    daemon, _, _ = _daemon(cfg, tmp_path, gh=gh)
    daemon.run(poll_seconds=0, stop=stop, clock=lambda: T0)
    assert gh.calls == Daemon.FAILURE_LIMIT + 6
    assert len(daemon.failures) == Daemon.FAILURE_LIMIT


# ---------------------------------------------------------------- failures


def test_the_executors_swallowed_failures_are_drained_into_the_snapshot(cfg, tmp_path):
    """One record, not two. The executor keeps HerdrErrors rather than raising
    them, and a deque nothing empties is a second, silent copy of a fact the
    operator never sees. The daemon takes them; the executor's is empty again."""
    herdr = _StubbornHerdr()
    daemon, gh, _ = _daemon(cfg, tmp_path, herdr=herdr)
    number = gh.create_issue("Fix LID", "b", CLAIMED)
    herdr.add_agent(task=number, status="dirty", change_seq=2, cost_usd=0.5)
    snapshot = daemon.tick(T0)
    assert list(daemon.executor.failures) == []
    assert any("could not stop agent" in line for line in snapshot.failures)
    assert any(f"task {number}" in line for line in daemon.failures)


def test_a_cleanup_failure_does_not_stop_the_tick(cfg, tmp_path):
    """The executor tolerates a HerdrError per effect; the point of draining it
    is that the rest of the tick still happened."""
    herdr = _StubbornHerdr()
    daemon, gh, _ = _daemon(cfg, tmp_path, herdr=herdr)
    number = gh.create_issue("Fix LID", "b", CLAIMED)
    herdr.add_agent(task=number, status="dirty", change_seq=2, cost_usd=0.5)
    daemon.tick(T0)
    assert "fleet:ready" in gh.get_issue(number)["labels"]
    assert daemon.state.spent_usd == pytest.approx(0.5)


# ---------------------------------------------------------------- signals


def test_sigterm_sets_the_stop_event(cfg, tmp_path):
    """A daemon that ignores SIGTERM cannot be stopped cleanly, and a supervisor
    that has to escalate to SIGKILL gets the crash path every time."""
    stop = threading.Event()
    with stop_on_signals(stop):
        signal.raise_signal(signal.SIGTERM)
    assert stop.is_set()


def test_sigint_sets_the_stop_event(cfg, tmp_path):
    """Ctrl-C at the terminal, the same clean stop a supervisor gets."""
    stop = threading.Event()
    with stop_on_signals(stop):
        signal.raise_signal(signal.SIGINT)
    assert stop.is_set()


def test_the_previous_handlers_come_back(cfg, tmp_path):
    """Installed for the life of a run, not for the life of the process. A
    handler left behind means the next Ctrl-C in the same process -- a test
    session, `fleet run --once` inside a wrapper -- sets an event nobody is
    waiting on, and the interpreter's own SIGINT never fires."""
    before = signal.getsignal(signal.SIGTERM)
    with stop_on_signals(threading.Event()):
        assert signal.getsignal(signal.SIGTERM) is not before
    assert signal.getsignal(signal.SIGTERM) is before


def test_a_signalled_run_finishes_the_tick_it_is_in(cfg, tmp_path):
    """Graceful means the effects of the tick in flight land. Exiting mid-tick
    would leave a task labelled claimed with no agent -- the ledger drift REAP
    then has to clean up, at the cost of an attempt."""
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    number = gh.create_issue("Fix LID", "b", READY)
    stop = threading.Event()
    with stop_on_signals(stop):
        daemon.run(
            poll_seconds=0,
            stop=stop,
            publish=lambda _: signal.raise_signal(signal.SIGTERM),
            clock=lambda: T0,
        )
    assert number in herdr.worktrees
    assert daemon.snapshot.tick == 1


# ---------------------------------------------------------------- shape


def test_the_daemon_holds_no_authoritative_state(cfg, tmp_path):
    """Parent §9. Everything the daemon carries is either reconstructible from
    the log and the ledger, or bounded and disposable -- so `kill -9` costs
    idle timers and nothing else."""
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    gh.create_issue("Fix LID", "b", READY)
    daemon.tick(T0)
    # Not `hasattr`: the point is that these are the only mutable fields, so a
    # tenth one added without a recovery story fails here.
    assert set(vars(daemon)) == {
        "cfg",
        "policy",
        "gh",
        "executor",
        "runlog",
        "run_id",
        "tick_number",
        "snapshot",
        "state",
        "failures",
    }


def test_a_drain_that_lifts_dispatches_again(cfg, tmp_path):
    """The drain filters `decide`'s output after the fold has already folded, so
    a filter that corrupted the state it came back with would leave the fleet
    unable to start work again once the price table is fixed -- and the operator
    who fixed it would have no way to tell."""
    daemon, gh, herdr = _daemon(cfg, tmp_path)
    number = gh.create_issue("Fix LID", "b", READY)
    herdr.unpriced_models = {"no price for model 'claude-new'"}
    assert daemon.tick(T0).draining is True
    herdr.unpriced_models = set()
    second = daemon.tick(T0 + timedelta(minutes=1))
    assert second.draining is False
    assert number in herdr.worktrees
    assert _kinds(tmp_path) == ["Drain", "Dispatch"]
