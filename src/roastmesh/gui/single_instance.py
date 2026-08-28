"""Single-instance guard for the GUI: a second `roastmesh-gui` launch
finds and focuses the window already running, instead of opening a
second one.

Not cosmetic: a second instance auto-starts its own `node serve` (same
identity as the first -- same ~/.local/share/roastmesh/feed -- and by
default the same database file), so two of those running at once means
two Iroh endpoints presenting the same identity and two processes writing
the same SQLite file concurrently. A user reported exactly the resulting
symptom: search breaking in the other, already-open instance.

A local TCP socket on 127.0.0.1 rather than a lock file: a lock file can
tell a second launch that it isn't first, but gives it no way to reach
the first one to ask it to raise its own window -- this needs to do both,
and a socket does both with one mechanism. Loopback TCP rather than a
Unix domain socket purely for portability -- `socket` behaves the same
on Linux/macOS/Windows either way, and Unix sockets don't.
"""
from __future__ import annotations

import socket
import threading
from collections.abc import Callable

PORT = 41892
_HOST = "127.0.0.1"


def another_instance_is_running(*, port: int = PORT, timeout: float = 0.3) -> bool:
    """True if a focus request was successfully delivered to an
    already-running instance -- the caller should exit immediately
    without creating a window in that case."""
    try:
        with socket.create_connection((_HOST, port), timeout=timeout) as sock:
            sock.sendall(b"focus\n")
        return True
    except OSError:
        return False


def start_focus_listener(on_focus_requested: Callable[[], None], *, port: int = PORT) -> socket.socket | None:
    """Best-effort: listen for focus requests from later launches, calling
    `on_focus_requested` (from a background thread -- the caller must
    marshal this back to the Tk main thread itself, e.g. via a Queue
    drained by `root.after`, same pattern gui/runner.py's stream_into
    already uses) each time one arrives. If the port can't be bound for
    any reason, this does nothing and returns None -- a second launch
    would then just open its own window instead of focusing this one,
    which is a worse UX but never a reason to refuse to start at all.
    Returns the listening socket (keep a reference for the process's
    lifetime; nothing needs to explicitly close it)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        # Windows only, and the opposite of SO_REUSEADDR rather than a variant
        # of it. On Windows SO_REUSEADDR means "let me take a port another
        # process already holds", so it defeats the very thing this guard
        # exists to detect: a second instance would bind successfully and two
        # nodes would run with one identity against one database. Confirmed by
        # the Windows test run, where the second bind returned a socket
        # instead of None.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
    else:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((_HOST, port))
        listener.listen(4)
    except OSError:
        listener.close()
        return None

    def _serve() -> None:
        while True:
            try:
                conn, _addr = listener.accept()
            except OSError:
                return  # listener closed (or, on some platforms, a transient accept error)
            try:
                conn.recv(64)
            except OSError:
                pass
            finally:
                conn.close()
            on_focus_requested()

    threading.Thread(target=_serve, daemon=True).start()
    return listener
