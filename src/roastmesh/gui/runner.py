"""Background command execution for the GUI.

Why subprocesses rather than calling the functions directly
-------------------------------------------------------------
The GUI shells out to the same `roastmesh` CLI a terminal user would run,
rather than importing and calling the underlying functions. Three reasons
(ported from roastlab's gui/runner.py, which this is adapted from -- same
reasoning applies here):

1. It reuses code that is already tested end to end. A GUI that re-implements
   argument handling would drift from the CLI and quietly disagree with it.
2. A hung or long-running operation (e.g. `peer sync`, which does real
   network I/O) is cancelled by killing a process -- simple and reliable,
   unlike interrupting arbitrary Python running on a thread.
3. A crash in a command takes down a subprocess, not the whole application.

The cost is that output arrives as text rather than objects. `search`
supports `--json` for exactly the cases (the results table) that need
structured data back; everything else is fine as text in a Console.

Cancellation
-------------
`Task.cancel()` terminates the process group, not just the immediate child.
SIGTERM first, then SIGKILL after a grace period.
"""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from roastmesh.gui.i18n import t


@dataclass
class Task:
    """One running command. Output is delivered through a queue that the UI
    thread drains on a timer, which is the only thread-safe way to get text
    from a worker into a tkinter widget."""

    argv: list[str]
    output: queue.Queue[tuple[str, str]] = field(default_factory=queue.Queue)
    _proc: subprocess.Popen | None = None
    _thread: threading.Thread | None = None
    _cancelled: bool = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, cwd: str | None = None) -> None:
        self._thread = threading.Thread(target=self._run, args=(cwd,), daemon=True)
        self._thread.start()

    def _run(self, cwd: str | None) -> None:
        try:
            self._proc = subprocess.Popen(
                self.argv,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # interleave, so failures appear in order
                text=True,
                # The CLI prints degree signs and, in French, accented text.
                # Without an explicit encoding this decodes as cp1252 on
                # Windows and either mangles the console output or raises
                # inside the read loop below.
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # Own process group, so cancel() can take down children too.
                **_process_group_kwargs(),
            )
        except FileNotFoundError as exc:
            self.output.put(("error", t("could not start: {error}", error=exc)))
            self.output.put(("done", "1"))
            return
        except Exception as exc:  # noqa: BLE001 -- surfaced to the user, never swallowed
            self.output.put(("error", t("could not start: {error}", error=repr(exc))))
            self.output.put(("done", "1"))
            return

        # Everything past this point must still guarantee a ("done", ...)
        # eventually lands in the queue, no matter what -- otherwise a
        # crash here kills this background thread silently (Python thread
        # exceptions just print to stderr) while stream_into's pump() waits
        # forever for a "done" that never arrives, leaving the GUI stuck
        # showing "running..." with no error.
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                self.output.put(("line", line.rstrip("\n")))
            code = self._proc.wait()
            if self._cancelled:
                self.output.put(("line", t("-- cancelled --")))
        except Exception as exc:  # noqa: BLE001 -- surfaced to the user, never swallowed
            self.output.put(("error", t("failed while reading output: {error}", error=repr(exc))))
            code = 1
        self.output.put(("done", str(code)))

    def cancel(self) -> None:
        """Stop the command, and its children, on either platform.

        Killing the whole tree matters more than it looks: `node serve` is the
        long-lived child, and leaving it behind means an invisible node that
        keeps serving, discovering and auto-publishing with no window attached
        -- a leak this project has already hit for real on Linux.

        The POSIX path signals the process group. On Windows there are no
        process groups in that sense and `os.killpg`/`os.getpgid`/`SIGKILL` do
        not exist at all -- they would raise AttributeError, which the old
        `except (ProcessLookupError, PermissionError)` did not catch, so every
        Cancel press and every window close would have thrown and left the app
        unclosable. `taskkill /T` is the Windows equivalent that reaches
        descendants; `proc.terminate()` alone would only kill the direct child.
        """
        self._cancelled = True
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                proc.wait(timeout=5)
                return
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired, OSError):
            pass  # already gone, or refused -- either way there is nothing left to do


def _process_group_kwargs() -> dict:
    """Popen kwargs that put the child where cancel() can reach its whole tree.

    POSIX gets its own session; Windows gets its own process group plus
    CREATE_NO_WINDOW. That last flag is not cosmetic polish: `roastmesh.exe` is
    a console binary, so without it every command the GUI runs flashes a black
    console window -- including the peer-list refresh, which fires every 30
    seconds for as long as the app is open.

    `start_new_session` is silently ignored on Windows rather than raising, so
    the old code left the child in the parent's group and the "cancel can take
    down children too" guarantee simply did not hold there.
    """
    if sys.platform == "win32":
        return {"creationflags": (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                  | getattr(subprocess, "CREATE_NO_WINDOW", 0))}
    return {"start_new_session": True}


def roastmesh_argv(*args: str) -> list[str]:
    """Build an argv that invokes this project's CLI with the SAME
    interpreter the GUI is running under.

    Using sys.executable rather than a bare "roastmesh" matters: this project
    is normally used from a virtualenv (.venv/bin/roastmesh), and a bare name
    would find the system Python's copy -- or nothing at all.

    Under a PyInstaller-frozen roastmesh-gui, sys.executable IS the frozen
    bootloader, not a Python interpreter that understands "-m roastmesh.cli"
    -- so instead shell out to the sibling `roastmesh` binary, which the
    packaged distribution always ships alongside it (see packaging/).
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        cli_name = "roastmesh.exe" if sys.platform == "win32" else "roastmesh"
        return [str(exe_dir / cli_name), *args]
    return [sys.executable, "-m", "roastmesh.cli", *args]


def stream_into(
    task: Task,
    widget_append: Callable[[str], None],
    on_done: Callable[[int], None],
    schedule: Callable[[int, Callable[[], None]], object],
    interval_ms: int = 120,
) -> None:
    """Drain a task's queue onto the UI thread.

    `schedule` is tkinter's `after`; this deliberately polls rather than
    pushing from the worker, because tkinter widgets may only be touched from
    the thread that created them.
    """

    def pump() -> None:
        finished: int | None = None
        drained: list[str] = []
        try:
            while True:
                kind, payload = task.output.get_nowait()
                if kind == "done":
                    finished = int(payload)
                elif kind == "error":
                    drained.append(t("ERROR: {message}", message=payload))
                else:
                    drained.append(payload)
        except queue.Empty:
            pass
        if drained:
            widget_append("\n".join(drained) + "\n")
        if finished is None:
            schedule(interval_ms, pump)
        else:
            on_done(finished)

    schedule(interval_ms, pump)


def describe(argv: Sequence[str]) -> str:
    """The command as a user could retype it in a terminal. Shown above every
    run: the GUI should never be a black box, and a user who wants to script
    something later needs to know what it actually did."""
    parts = []
    for a in argv:
        parts.append(f'"{a}"' if " " in a else a)
    return " ".join(parts)
