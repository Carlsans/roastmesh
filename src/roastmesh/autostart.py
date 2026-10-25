"""Starting the background server (background.py) automatically.

Per user and without admin rights on both platforms, matching how roastmesh
installs everywhere else (install.sh writes to ~/.local, the Windows setup to
%LOCALAPPDATA%):

* Linux: a systemd *user* unit, plus `loginctl enable-linger` so it starts at
  boot instead of at first login. Linger needs no root for your own account
  on most systems; where it is refused the unit still works, from login, and
  `status()` says so rather than claiming "at boot".
* Windows: a Task Scheduler task triggered at *sign-in*. A true pre-login
  start would need a Windows service, which needs administrator rights to
  install and a service wrapper this program does not have, so sign-in is the
  honest limit of what a per-user install can do.

Both are driven through the system tools (systemctl, loginctl, schtasks) with
the command lines kept in small pure functions, so they can be checked
without a Windows machine or touching the real service manager.
"""
from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from roastmesh import background
from roastmesh.procenv import external_subprocess_env

UNIT_NAME = "roastmesh-background.service"
TASK_NAME = "roastmesh-background"


class AutostartError(Exception):
    """Something the user can act on; the message says what."""


@dataclass
class Status:
    supported: bool
    enabled: bool = False
    running: bool = False
    # "boot" (starts with the machine), "login" (starts when you sign in), or
    # "" when not enabled.
    trigger: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _run(argv: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess:
    # These are all external system tools (systemctl, loginctl, schtasks,
    # taskkill, whoami), never roastmesh itself -- see procenv.py for why a
    # frozen build's environment needs cleaning up before launching one.
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=external_subprocess_env(),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AutostartError(f"could not run {argv[0]}: {exc}") from exc


def _check(proc: subprocess.CompletedProcess, what: str) -> None:
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise AutostartError(f"{what} failed: {message}")


# -- Linux: systemd user unit ----------------------------------------------

def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT_NAME


def _systemd_quote(arg: str) -> str:
    """One ExecStart word. Quoted always: systemd splits on whitespace and
    expands %-specifiers and $ variables, none of which a path should trigger."""
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def render_unit(cli: list[str]) -> str:
    exec_start = " ".join(_systemd_quote(a) for a in [*cli, "background", "run"])
    return (
        "[Unit]\n"
        "Description=roastmesh background node (serves other nodes; pauses while the app window is open)\n"
        "Documentation=https://github.com/Carlsans/roastmesh\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["systemctl", "--user", *args])


def _linger_enabled() -> bool:
    if not shutil.which("loginctl"):
        return False
    try:
        proc = _run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger", "--value"], timeout=10)
    except AutostartError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "yes"


def _linux_status() -> Status:
    if not shutil.which("systemctl"):
        return Status(False, detail="systemd was not found on this system")
    if not unit_path().exists():
        return Status(True)
    enabled = _systemctl("is-enabled", UNIT_NAME).returncode == 0
    running = _systemctl("is-active", UNIT_NAME).returncode == 0
    linger = _linger_enabled()
    detail = "" if linger or not enabled else (
        "it starts when you log in, not at boot -- run: sudo loginctl enable-linger "
        f"{getpass.getuser()}")
    return Status(True, enabled=enabled, running=running,
                  trigger=("boot" if linger else "login") if enabled else "", detail=detail)


def _linux_enable() -> Status:
    if not shutil.which("systemctl"):
        raise AutostartError("systemd was not found on this system, so there is nothing to start "
                             "roastmesh at boot. Run `roastmesh background run` from your own init instead.")
    unit_dir().mkdir(parents=True, exist_ok=True)
    unit_path().write_text(render_unit(background.cli_argv()), encoding="utf-8")
    _check(_systemctl("daemon-reload"), "systemctl daemon-reload")
    _check(_systemctl("enable", UNIT_NAME), "systemctl enable")
    # restart, not start: enabling again after an upgrade must pick up the new
    # binary, and `start` leaves an already-running old one alone.
    _check(_systemctl("restart", UNIT_NAME), "systemctl restart")
    if not _linger_enabled() and shutil.which("loginctl"):
        _run(["loginctl", "enable-linger", getpass.getuser()], timeout=15)   # best effort
    return _linux_status()


def _linux_disable() -> Status:
    if unit_path().exists():
        _systemctl("disable", "--now", UNIT_NAME)   # tolerate "not loaded": the goal is that it is gone
        unit_path().unlink(missing_ok=True)
        _systemctl("daemon-reload")
    return _linux_status()


# -- Windows: Task Scheduler -----------------------------------------------

def windows_command() -> tuple[str, str]:
    """(program, arguments) the sign-in task runs. The windowed GUI binary with
    --background rather than roastmesh.exe: that one is a console program, so
    Windows would open a black console window at every sign-in, and a hidden
    task does not prevent it."""
    exe = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        return str(exe.parent / "roastmesh-gui.exe"), "--background"
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe), "-m roastmesh.gui.app --background"


def render_task_xml(program: str, arguments: str, user: str) -> str:
    """Task definition. XML rather than `schtasks /Create /SC ONLOGON`, which
    stops the task after 72 hours by default, would trigger on *any* user's
    sign-in, and has no switch for the battery settings below."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>roastmesh background node: serves other nodes; pauses while the app window is open.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(program)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _windows_user() -> str:
    """The account the task belongs to, as Task Scheduler resolves it.

    `whoami` (COMPUTER\\user), not %USERDOMAIN%: over ssh, and on machines in
    a workgroup, USERDOMAIN says WORKGROUP, which names no account, and the
    task is rejected with "no mapping between account names and security
    IDs" -- found on a real PC. The bare user name is the fallback, which
    Task Scheduler resolves on its own.
    """
    try:
        proc = _run(["whoami"], timeout=10)
        name = proc.stdout.strip()
        if proc.returncode == 0 and name:
            return name
    except AutostartError:
        pass
    return os.environ.get("USERNAME") or getpass.getuser()


def _task_exists() -> bool:
    return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0


def _windows_status() -> Status:
    if not _task_exists():
        return Status(True)
    return Status(True, enabled=True, running=background.supervisor_pid() is not None, trigger="login")


def _windows_enable() -> Status:
    program, arguments = windows_command()
    # Stop a copy from before an upgrade first, or the new task's start is
    # ignored (IgnoreNew) and the old binary keeps running.
    _windows_stop()
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "task.xml"
        xml.write_text(render_task_xml(program, arguments, _windows_user()), encoding="utf-16")
        _check(_run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml), "/F"]), "schtasks /Create")
    _check(_run(["schtasks", "/Run", "/TN", TASK_NAME]), "schtasks /Run")
    return _windows_status()


def _windows_disable() -> Status:
    _windows_stop()
    if _task_exists():
        _check(_run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]), "schtasks /Delete")
    return _windows_status()


def _windows_stop() -> None:
    pid = background.supervisor_pid()
    if pid is not None:
        # /T takes the node it started along, so no binary is left locked.
        _run(["taskkill", "/F", "/T", "/PID", str(pid)])
    node = background.read_state().get("node")
    if isinstance(node, int) and background.pid_alive(node):
        _run(["taskkill", "/F", "/T", "/PID", str(node)])
    # A killed supervisor cannot tidy up after itself.
    background._write_state(None, None)


# -- public API ------------------------------------------------------------

def status() -> Status:
    if sys.platform == "win32":
        return _windows_status()
    if sys.platform.startswith("linux"):
        return _linux_status()
    return Status(False, detail="running in the background is available on Linux and Windows")


def enable() -> Status:
    if sys.platform == "win32":
        return _windows_enable()
    if sys.platform.startswith("linux"):
        return _linux_enable()
    raise AutostartError("running in the background is available on Linux and Windows")


def disable() -> Status:
    if sys.platform == "win32":
        return _windows_disable()
    if sys.platform.startswith("linux"):
        return _linux_disable()
    return status()


def stop() -> None:
    """Stop it now without disabling it -- how an installer frees the binaries
    it is about to replace."""
    if sys.platform == "win32":
        _windows_stop()
    elif sys.platform.startswith("linux") and unit_path().exists():
        _systemctl("stop", UNIT_NAME)


def start() -> None:
    if sys.platform == "win32":
        if _task_exists():
            _check(_run(["schtasks", "/Run", "/TN", TASK_NAME]), "schtasks /Run")
    elif sys.platform.startswith("linux") and unit_path().exists():
        _check(_systemctl("start", UNIT_NAME), "systemctl start")
