"""The state socket. Newline-delimited JSON over a Unix domain socket.

Named `statesocket` rather than `socket` so it cannot shadow the stdlib module
it imports.

Parent §2: if the client were the orchestrator, closing it would stop the fleet.
So the daemon owns the loop and clients attach and detach -- each connection is
served exactly one rendered snapshot and then closed. Nothing authoritative
lives here: the socket serves a render of the last completed tick, and losing
it costs a client one refresh, never the fleet a tick.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from collections.abc import Callable
from pathlib import Path

#: Bounds the server's accept() and each connection's send -- long enough that
#: a live client is never starved, short enough that `stop` is noticed
#: promptly and a client that connects and never reads cannot pin the loop.
_TIMEOUT = 0.5
#: Bounds the client's connect+read. Generous relative to `_TIMEOUT` because a
#: slow daemon under load should still answer before this task's caller gives
#: up and reports the fleet dead.
_CLIENT_TIMEOUT = 2.0
#: Bounds the startup probe of a pre-existing socket path. Unix-domain
#: connect() either succeeds, is refused, or finds nothing there -- all fast,
#: local, non-blocking operations -- so this only guards against the
#: pathological case.
_PROBE_TIMEOUT = 0.2
_BACKLOG = 8


class NotRunning(Exception):
    """No daemon is listening on that socket."""


def _replace_if_stale(path: Path) -> None:
    """A daemon killed with -9 leaves its socket file behind, and bind()
    refuses an existing path -- unhandled, the fleet cannot be restarted after
    exactly the failure it is designed to survive.

    But the file might not be stale: another daemon could already be serving
    it. So this probes first, as a client would, rather than unlinking on
    sight. A refusal or a missing listener (`OSError` -- `ConnectionRefused`,
    `FileNotFoundError`, or a `connect()` to a plain file, which Unix sockets
    also answer with `ConnectionRefusedError`) means nothing is home and the
    path is safe to reclaim. A connection that succeeds means a live daemon
    answered; the file is left alone and the `bind()` that follows raises
    `OSError: Address already in use` instead of two daemons silently sharing
    one path.

    That guarantee -- "a live listener is left alone" -- holds only for the
    sequential case this hazard was written for: SIGKILL, then a restart,
    never two daemons alive on this path at once. It does NOT close the
    window between this probe and the caller's `bind()`: if a second `serve()`
    raced in and bound during that window, this call would unlink that second
    daemon's brand-new socket file and bind over it, orphaning a live server
    that keeps running but is no longer reachable at `path`. Nothing else in
    this codebase (no pidfile, no lock) stops two daemons from starting
    concurrently in the first place, so closing this window here would be
    solving a problem this module cannot see the rest of. It is named rather
    than fixed so a future reader does not build on a promise this code
    cannot keep.
    """
    if not path.exists():
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(_PROBE_TIMEOUT)
    try:
        probe.connect(str(path))
    except OSError:
        # Nobody answered -- stale file from a killed daemon (or not even a
        # socket). Safe to reclaim.
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    else:
        # A live daemon answered. Leave the file alone; the `bind()` that
        # follows will raise "Address already in use" instead of two daemons
        # silently sharing one path.
        pass
    finally:
        probe.close()


def serve(
    path: Path,
    get_snapshot: Callable[[], object | None],
    stop: threading.Event,
) -> threading.Thread:
    """Start the accept loop in a background thread and return it, already
    listening -- `bind()` and `listen()` happen here, on the caller's thread,
    before the thread is started, so by the time `serve()` returns a `request`
    to `path` can succeed with no sleep or readiness poll required.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace_if_stale(path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        server.listen(_BACKLOG)
    except OSError:
        server.close()
        raise
    # A timeout rather than a blocking accept: without it the thread cannot
    # notice `stop` and the daemon hangs on shutdown until someone connects.
    server.settimeout(_TIMEOUT)

    def loop() -> None:
        try:
            while not stop.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    # Bounds a client that connects and never reads: without
                    # this, a full send buffer blocks `sendall` and pins the
                    # accept loop against one unresponsive client forever.
                    connection.settimeout(_TIMEOUT)
                    try:
                        # `get_snapshot()` reads a single attribute reference
                        # the tick loop replaces with one assignment
                        # (`self.snapshot = ...`) -- in CPython that read and
                        # that write are each a single bytecode under the
                        # GIL, so this always observes either the previous
                        # complete snapshot or the next one, never a
                        # half-built object.
                        #
                        # `to_json()` and `json.dumps()` can still raise on a
                        # bad payload -- `model.py`'s own `to_json()`
                        # docstring warns a tuple or a `Path` in the rendered
                        # rows is "a TypeError inside a thread nothing is
                        # watching." Both are guarded here, scoped to
                        # `Exception` rather than `OSError`: a render or
                        # serialisation bug is not a transport error, but it
                        # must still cost this one request, not the socket --
                        # unguarded, it would unlink `path` and leave the
                        # daemon alive but unreachable, exactly like the
                        # unguarded broken pipe below.
                        snapshot = get_snapshot()
                        payload = snapshot.to_json() if snapshot is not None else {}
                        body = (json.dumps(payload) + "\n").encode("utf-8")
                    except Exception:
                        continue
                    try:
                        connection.sendall(body)
                    except OSError:
                        # A client that vanished mid-write (broken pipe,
                        # connection reset) or was simply too slow to read
                        # before the timeout above. That costs this one
                        # client its refresh, not the server: the accept loop
                        # must survive it so the next `fleet status` is still
                        # served.
                        continue
        finally:
            server.close()
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    thread = threading.Thread(target=loop, name="fleet-statesocket", daemon=True)
    thread.start()
    return thread


def request(path: Path) -> dict:
    """Read the daemon's last completed tick. Raises `NotRunning` -- never a
    bare `ConnectionRefusedError` or `FileNotFoundError` -- when nothing
    answers, so a CLI caller can say so rather than inventing state."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(_CLIENT_TIMEOUT)
    try:
        try:
            client.connect(str(path))
        except OSError as exc:
            raise NotRunning(f"no fleet daemon listening on {path}") from exc
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        client.close()
    return json.loads(b"".join(chunks).decode("utf-8") or "{}")
