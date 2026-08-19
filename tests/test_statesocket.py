from __future__ import annotations

import socket
import threading

import pytest

from fleet.model import FleetSnapshot
from fleet.statesocket import NotRunning, request, serve

SNAPSHOT = FleetSnapshot(
    run_id="r1",
    tick=3,
    at="2026-08-18T02:00:00+00:00",
    rows=({"task": 1, "state": "claimed"},),
    spent_usd=1.5,
    fleet_usd=25.0,
    free_slots=5,
    held_areas=("eval",),
)


def _served(tmp_path, snapshot=SNAPSHOT):
    path = tmp_path / "fleet.sock"
    stop = threading.Event()
    thread = serve(path, lambda: snapshot, stop)
    return path, stop, thread


def test_a_client_reads_the_last_completed_tick(tmp_path):
    path, stop, thread = _served(tmp_path)
    try:
        assert request(path)["tick"] == 3
        assert request(path)["held_areas"] == ["eval"]
    finally:
        stop.set()
        thread.join(timeout=2)


def test_two_clients_can_attach_in_turn(tmp_path):
    """Clients attach and detach; the daemon owns the loop. If serving one
    client ended the server, `watch fleet status` would stop the fleet's own
    status after a single refresh."""
    path, stop, thread = _served(tmp_path)
    try:
        assert request(path)["tick"] == 3
        assert request(path)["tick"] == 3
    finally:
        stop.set()
        thread.join(timeout=2)


def test_a_client_with_no_daemon_says_so_rather_than_inventing_state(tmp_path):
    """Rendering the ledger instead would leave a reader unable to tell a dead
    fleet from an idle one -- which is the single most important thing `fleet
    status` has to communicate at 7am."""
    with pytest.raises(NotRunning):
        request(tmp_path / "absent.sock")


def test_a_tick_that_has_not_happened_yet_is_null(tmp_path):
    path = tmp_path / "fleet.sock"
    stop = threading.Event()
    thread = serve(path, lambda: None, stop)
    try:
        assert request(path) == {}
    finally:
        stop.set()
        thread.join(timeout=2)


def test_a_live_daemons_socket_is_not_stolen(tmp_path):
    """The stale-socket cleanup must not unlink a path a live daemon is still
    serving -- that would let a second `serve()` silently take over the path
    out from under the first, so a client could be talking to either daemon
    depending on timing. `serve` probes before unlinking; finding a live
    listener, it leaves the file alone and lets the OS refuse the second
    `bind()` instead."""
    path, stop, thread = _served(tmp_path)
    try:
        other_stop = threading.Event()
        with pytest.raises(OSError):
            serve(path, lambda: SNAPSHOT, other_stop)
        # The first daemon is unharmed: still there, still answering.
        assert request(path)["tick"] == 3
    finally:
        stop.set()
        thread.join(timeout=2)


class _UnserializableSnapshot:
    """A stand-in for a `FleetSnapshot` whose `to_json()` returns a payload
    `json.dumps` cannot handle -- `model.py`'s own docstring warns this is
    "a TypeError inside a thread nothing is watching"."""

    def to_json(self):
        return {"bad": {1, 2, 3}}  # a set is not JSON serialisable


def test_a_render_error_costs_only_that_request(tmp_path):
    """`get_snapshot()` itself can raise -- a bug in the daemon's own state,
    not a vanished client. Uncaught, it would kill the accept-loop thread and
    unlink the socket while the daemon keeps ticking, leaving every later
    `fleet status` raising `NotRunning` against a live daemon forever. One bad
    render must cost that one request, not the socket."""
    path = tmp_path / "fleet.sock"
    stop = threading.Event()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return SNAPSHOT

    thread = serve(path, flaky, stop)
    try:
        assert request(path) == {}  # the failed request gets nothing, not a crash
        assert request(path)["tick"] == 3  # the next request is still served
    finally:
        stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_a_serialization_error_costs_only_that_request(tmp_path):
    """`snapshot.to_json()` or `json.dumps()` can raise on a bad payload just
    as easily as `get_snapshot()` can. Same failure, same fix: it must cost
    the one request that hit it, not every request after it."""
    path = tmp_path / "fleet.sock"
    stop = threading.Event()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return _UnserializableSnapshot() if calls["n"] == 1 else SNAPSHOT

    thread = serve(path, flaky, stop)
    try:
        assert request(path) == {}
        assert request(path)["tick"] == 3
    finally:
        stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_a_client_that_vanishes_mid_write_costs_only_itself(tmp_path):
    """A client that connects and closes without reading makes `sendall`
    raise `BrokenPipeError`. A tick spent on a publish failure is a tick the
    operator does not get a snapshot from -- so that failure must be caught
    inside the accept loop, not left to escape and kill the serving thread.
    An unhandled escape would unlink the socket in its `finally` and leave
    every later, well-behaved client raising `NotRunning` against a daemon
    that is still alive."""
    path, stop, thread = _served(tmp_path)
    try:
        vanished = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        vanished.connect(str(path))
        vanished.close()  # never reads
        assert request(path)["tick"] == 3
    finally:
        stop.set()
        thread.join(timeout=2)


def test_stop_ends_the_serving_thread(tmp_path):
    """`stop` must actually end the accept loop, not just be observed and
    ignored -- a lingering thread is invisible until the daemon has restarted
    many times over a long run and the leak compounds. `join(timeout=...)`
    alone does not prove this: it returns whether or not the thread died, so
    the assertion has to check `is_alive()` itself to fail loudly rather than
    pass silently on a thread that never exits."""
    path, stop, thread = _served(tmp_path)
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_a_stale_socket_file_is_replaced(tmp_path):
    """A daemon killed with -9 leaves the socket file behind, and bind() refuses
    an existing path. Unhandled, the fleet cannot be restarted after exactly the
    failure it is designed to survive."""
    path = tmp_path / "fleet.sock"
    path.write_text("")
    stop = threading.Event()
    thread = serve(path, lambda: SNAPSHOT, stop)
    try:
        assert request(path)["tick"] == 3
    finally:
        stop.set()
        thread.join(timeout=2)
