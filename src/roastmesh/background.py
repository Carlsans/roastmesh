"""The always-on background server: what `roastmesh background run` does.

It runs `node serve` with the settings the GUI saved (gui_config.json), so
the machine keeps answering other nodes after boot with no window open. The
OS side -- starting this at boot / sign-in -- lives in autostart.py.

One identity, one database, one set of ports
--------------------------------------------
The GUI runs its own `node serve` for as long as its window is open, and a
second node with the same identity against the same SQLite file is the
failure single_instance.py exists to prevent. So this process is a small
supervisor rather than a bare `node serve`: while the GUI holds its
single-instance port the background node is stopped, and it comes back about
a second after the window closes. Watching that port (rather than being told
by the GUI) is deliberate -- a GUI that crashed or was killed releases it
too, so the background node can never stay down because nobody remembered to
resume it.

The supervisor also keeps `background.json` (its pid and the node's) so the
GUI can wait for the handoff, and the installers can stop everything before
replacing the binaries.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from roastmesh.gui import single_instance
from roastmesh.index.db import default_db_path
from roastmesh.paths import data_dir, default_devices_dir, default_watch_dir

POLL_SECONDS = 1.0
# Wait this long before restarting a node that exited on its own, so a node
# that cannot start (bad config, port held by something else) retries
# gently instead of spinning.
RESTART_DELAY_SECONDS = 5.0
_LOG_ROTATE_BYTES = 1_000_000


def settings_path() -> Path:
    return data_dir() / "gui_config.json"


def state_path() -> Path:
    return data_dir() / "background.json"


def log_path() -> Path:
    return data_dir() / "background.log"


def valid_public_port(value: object) -> str:
    """A port, or "" -- never anything that would become a bad CLI argument.

    Whatever survives here is handed straight to `node serve --public-port`,
    so junk in the config file must not turn into a serve process that
    refuses to start; an unusable value is simply no value.
    """
    text = str(value).strip().lower()
    if text == "auto":
        return "auto"
    try:
        port = int(text)
    except (TypeError, ValueError):
        return ""
    return str(port) if 1 <= port <= 65535 else ""


@dataclass(frozen=True)
class Settings:
    """The slice of the GUI's preferences that decides how `node serve` runs."""

    db_path: str
    watch_dir: str
    wan_discovery: bool = True
    public_port: str = ""
    device_sync: bool = True
    devices_dir: str = ""


def load_settings() -> Settings:
    """What the GUI last saved, or its defaults when it never ran (a headless
    box that only ever had the background service). Read fresh each time the
    node starts, so a change made in Settings applies the next time it does."""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    return Settings(
        db_path=data.get("db_path") or str(default_db_path()),
        watch_dir=data.get("watch_dir") or str(default_watch_dir()),
        wan_discovery=bool(data.get("wan_discovery_enabled", True)),
        public_port=valid_public_port(data.get("public_port")),
        device_sync=bool(data.get("enable_device_sync", True)),
        devices_dir=str(data.get("devices_dir") or ""),
    )


def node_serve_args(settings: Settings, *, debug: bool = False) -> list[str]:
    """Everything after the `roastmesh` executable for a `node serve` run.

    The one place that turns settings into flags, shared by the GUI's own
    node and the background one so the two can never disagree about how a
    setting is applied.
    """
    args = ["--db", settings.db_path, "node", "serve", "--publish-watch-dir", settings.watch_dir]
    if debug:
        args.append("--debug")
    if settings.wan_discovery:
        args.append("--wan-discovery")
        port = settings.public_port.strip().lower()
        if port == "auto":
            # No --wan-port here: the router picks the external number, so
            # pinning both would only work if we happened to guess the one
            # it was going to grant.
            args += ["--public-port", "auto"]
        elif port.isdigit():
            # Both, and the same number: --wan-port is the socket we listen
            # on, --public-port is what we tell other nodes to use. A
            # forward is only useful if the port it delivers to is ours.
            args += ["--wan-port", port, "--public-port", port]
    if settings.device_sync:
        devices_dir = settings.devices_dir.strip()
        if devices_dir:
            args += ["--devices-dir", devices_dir]
    else:
        args.append("--no-device-sync")
    return args


def cli_argv() -> list[str]:
    """How to invoke this project's CLI from here -- the frozen binary itself,
    or this interpreter running the module (same split as gui/runner.py)."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return [str(exe_dir / ("roastmesh.exe" if sys.platform == "win32" else "roastmesh"))]
    return [sys.executable, "-m", "roastmesh.cli"]


# -- shared state ----------------------------------------------------------

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def read_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(supervisor: int | None, node: int | None) -> None:
    path = state_path()
    try:
        if supervisor is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"supervisor": supervisor, "node": node}), encoding="utf-8")
    except OSError:
        pass  # bookkeeping only -- never a reason to take the server down


def _recorded_pid(key: str) -> int | None:
    pid = read_state().get(key)
    return pid if isinstance(pid, int) and pid_alive(pid) else None


def supervisor_pid() -> int | None:
    """The running supervisor's pid, or None (a stale file left by a crash
    names a process that is gone, which pid_alive catches)."""
    return _recorded_pid("supervisor")


def node_is_running() -> bool:
    """True while the background node holds the identity and ports -- what
    the GUI waits out before starting its own."""
    return _recorded_pid("node") is not None


# -- the supervisor --------------------------------------------------------

def _terminate(proc: subprocess.Popen) -> None:
    """Stop the node and anything it spawned, on either platform (the same
    approach as gui/runner.py's Task.cancel)."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proc.wait(timeout=5)
            return
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired, OSError):
        pass  # already gone, or refused -- nothing left to do


def _open_log():
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > _LOG_ROTATE_BYTES:
            path.replace(path.with_suffix(".log.1"))
    except OSError:
        pass
    return open(path, "ab")


def _spawn_node() -> subprocess.Popen:
    from roastmesh.gui.runner import _process_group_kwargs

    argv = [*cli_argv(), *node_serve_args(load_settings())]
    log = _open_log()
    try:
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting: {' '.join(argv)}\n".encode())
        log.flush()
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                **_process_group_kwargs())
    finally:
        log.close()  # the child holds its own handle


def run_forever(*, poll: float = POLL_SECONDS, gui_port: int = single_instance.PORT,
                stop: threading.Event | None = None, spawn=_spawn_node) -> int:
    """Keep a node serving, except while the GUI window is open. Returns when
    `stop` is set (or on SIGTERM/SIGINT when this is the main thread)."""
    if supervisor_pid() not in (None, os.getpid()):
        print("roastmesh background is already running", file=sys.stderr)
        return 1
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_a: stop.set())
            except (ValueError, OSError):
                pass

    child: subprocess.Popen | None = None
    next_start = 0.0
    _write_state(os.getpid(), None)
    try:
        while not stop.is_set():
            gui_open = single_instance.port_is_held(gui_port)
            if child is not None and (gui_open or child.poll() is not None):
                _terminate(child)
                if not gui_open:
                    next_start = time.monotonic() + RESTART_DELAY_SECONDS  # it died on its own
                child = None
                _write_state(os.getpid(), None)
            if child is None and not gui_open and time.monotonic() >= next_start:
                try:
                    child = spawn()
                except OSError:
                    next_start = time.monotonic() + RESTART_DELAY_SECONDS
                else:
                    _write_state(os.getpid(), child.pid)
            stop.wait(poll)
    finally:
        if child is not None:
            _terminate(child)
        _write_state(None, None)
    return 0
