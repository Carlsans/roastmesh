from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from roastmesh import autostart


def test_unit_quotes_every_word_and_escapes_specifiers() -> None:
    unit = autostart.render_unit(["/home/me/My Tools/roastmesh", "--odd%arg$"])
    assert 'ExecStart="/home/me/My Tools/roastmesh" "--odd%%arg$$" "background" "run"' in unit
    assert "WantedBy=default.target" in unit
    assert "Restart=on-failure" in unit


def test_unit_for_a_module_run() -> None:
    unit = autostart.render_unit([sys.executable, "-m", "roastmesh.cli"])
    # systemd escapes backslashes, which only a Windows interpreter path has
    exe = sys.executable.replace("\\", "\\\\")
    assert f'"{exe}" "-m" "roastmesh.cli" "background" "run"' in unit


def test_unit_path_honours_xdg_config_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert autostart.unit_path() == tmp_path / "systemd" / "user" / "roastmesh-background.service"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Path.home() reads this on Windows
    assert autostart.unit_path() == tmp_path / ".config" / "systemd" / "user" / "roastmesh-background.service"


def test_task_xml_is_well_formed_and_never_times_out() -> None:
    xml = autostart.render_task_xml(r"C:\Users\A & B\roastmesh-gui.exe", "--background", r"PC\A & B")
    root = ET.fromstring(xml.split("?>", 1)[1])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:Exec/t:Command", ns).text == r"C:\Users\A & B\roastmesh-gui.exe"
    assert root.find(".//t:Exec/t:Arguments", ns).text == "--background"
    # schtasks' own default is to kill the task after 72 hours
    assert root.find(".//t:ExecutionTimeLimit", ns).text == "PT0S"
    # only this user's sign-in, not anyone's
    assert root.find(".//t:LogonTrigger/t:UserId", ns).text == r"PC\A & B"
    assert root.find(".//t:RunLevel", ns).text == "LeastPrivilege"


def test_windows_command_uses_the_windowed_binary_when_frozen(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "roastmesh.exe"))
    program, args = autostart.windows_command()
    assert Path(program).name == "roastmesh-gui.exe" and args == "--background"


class Recorder:
    """Replaces subprocess.run for the system tools."""

    def __init__(self, fail: set[str] | None = None, stdout: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail or set()
        self.stdout = stdout or {}

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        key = " ".join(argv)
        if argv == ["whoami"]:
            return subprocess.CompletedProcess(argv, 0, stdout="desktop-abc\\carl\r\n", stderr="")
        code = 1 if any(f in key for f in self.fail) else 0
        return subprocess.CompletedProcess(argv, code, stdout=next(
            (v for k, v in self.stdout.items() if k in key), ""), stderr="boom" if code else "")


@pytest.fixture
def linux(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(autostart.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(autostart.getpass, "getuser", lambda: "carl")
    rec = Recorder(stdout={"Linger": "yes\n"})
    monkeypatch.setattr(autostart.subprocess, "run", rec)
    return rec


def test_linux_status_is_disabled_without_a_unit_and_never_touches_systemctl(linux) -> None:
    st = autostart.status()
    assert st.supported and not st.enabled
    assert linux.calls == []


def test_linux_enable_writes_the_unit_enables_restarts_and_reports_boot(linux) -> None:
    st = autostart.enable()
    assert autostart.unit_path().exists()
    assert 'background" "run"' in autostart.unit_path().read_text()
    verbs = [c[2] for c in linux.calls if c[:2] == ["systemctl", "--user"]]
    assert verbs[:3] == ["daemon-reload", "enable", "restart"]
    assert st.enabled and st.running and st.trigger == "boot" and st.detail == ""


def test_linux_without_linger_says_login_and_how_to_fix_it(linux) -> None:
    linux.stdout = {"Linger": "no\n"}
    st = autostart.enable()
    assert st.trigger == "login"
    assert "enable-linger carl" in st.detail
    assert ["loginctl", "enable-linger", "carl"] in linux.calls    # it did try


def test_linux_enable_surfaces_a_systemctl_failure(linux) -> None:
    linux.fail = {"systemctl --user enable"}
    with pytest.raises(autostart.AutostartError, match="systemctl enable failed: boom"):
        autostart.enable()


def test_linux_disable_removes_the_unit(linux) -> None:
    autostart.enable()
    st = autostart.disable()
    assert not autostart.unit_path().exists()
    assert not st.enabled
    assert any(c[:4] == ["systemctl", "--user", "disable", "--now"] for c in linux.calls)


def test_linux_disable_when_never_enabled_is_quietly_fine(linux) -> None:
    assert autostart.disable().enabled is False
    assert linux.calls == []


def test_linux_without_systemd_is_unsupported_and_enable_explains(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(autostart.shutil, "which", lambda name: None)
    assert autostart.status().supported is False
    with pytest.raises(autostart.AutostartError, match="systemd"):
        autostart.enable()


def test_other_platforms_are_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert autostart.status().supported is False
    with pytest.raises(autostart.AutostartError):
        autostart.enable()


@pytest.fixture
def windows(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "roastmesh.exe"))
    # USERDOMAIN is deliberately wrong, as it is over ssh on a workgroup PC
    monkeypatch.setenv("USERDOMAIN", "WORKGROUP")
    monkeypatch.setenv("USERNAME", "carl")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    created: list[str] = []
    rec = Recorder()
    real = rec.__call__

    def run(argv, **kw):
        if argv[:2] == ["schtasks", "/Create"]:
            created.append(Path(argv[argv.index("/XML") + 1]).read_bytes().decode("utf-16"))
        return real(argv, **kw)
    monkeypatch.setattr(autostart.subprocess, "run", run)
    rec.created = created
    return rec


def test_windows_enable_registers_the_task_from_xml_then_runs_it(windows) -> None:
    st = autostart.enable()
    create = next(c for c in windows.calls if c[:2] == ["schtasks", "/Create"])
    assert create[:4] == ["schtasks", "/Create", "/TN", "roastmesh-background"]
    assert "/F" in create
    assert 'roastmesh-gui.exe' in windows.created[0] and '--background' in windows.created[0]
    # the account comes from whoami, never from the (unreliable) USERDOMAIN
    assert "<UserId>desktop-abc\\carl</UserId>" in windows.created[0]
    assert "WORKGROUP" not in windows.created[0]
    assert ["schtasks", "/Run", "/TN", "roastmesh-background"] in windows.calls
    assert st.enabled and st.trigger == "login"


def test_windows_disable_deletes_the_task(windows) -> None:
    autostart.disable()
    assert ["schtasks", "/Delete", "/TN", "roastmesh-background", "/F"] in windows.calls


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell command as a stand-in systemctl")
def test_run_never_leaks_a_frozen_ld_library_path_to_the_child(monkeypatch) -> None:
    """The real incident this guards: `roastmesh background enable` on a
    frozen Linux build ran `systemctl --user daemon-reload` with the
    bundle's own LD_LIBRARY_PATH still set, and the system's systemd
    picked up roastmesh's bundled libcrypto instead of its own, failing
    with "version `OPENSSL_3.4.0' not found". A real subprocess (not the
    Recorder stand-in above) so the child's own view of the environment is
    what's checked, not just what autostart._run was handed."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxxxx")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)

    proc = autostart._run(["sh", "-c", 'echo "LD=[$LD_LIBRARY_PATH]"'])

    assert proc.stdout.strip() == "LD=[]"


def test_windows_status_disabled_when_no_task(windows) -> None:
    windows.fail = {"/Query"}
    assert autostart.status().enabled is False
