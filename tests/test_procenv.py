"""Unit tests for roastmesh.procenv.external_subprocess_env.

Shared by gui/app.py (xdg-open, flatpak, Artisan) and autostart.py
(systemctl, loginctl, schtasks) -- both launch external, non-roastmesh
programs from a possibly-frozen process, and both hit the same real bug:
a PyInstaller-frozen build's LD_LIBRARY_PATH (pointing at its own bundled
.so files) leaking into the child. See tests/test_gui_opener.py for the
xdg-open/Artisan integration tests, and tests/test_autostart.py for the
systemctl one -- this file is just the function in isolation.
"""
from __future__ import annotations

from roastmesh.procenv import external_subprocess_env


def test_external_subprocess_env_is_none_when_not_frozen(monkeypatch) -> None:
    monkeypatch.delattr("sys.frozen", raising=False)
    assert external_subprocess_env() is None


def test_external_subprocess_env_drops_ld_library_path_when_frozen_with_no_original(monkeypatch) -> None:
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxxxx")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)

    env = external_subprocess_env()

    assert env is not None
    assert "LD_LIBRARY_PATH" not in env


def test_external_subprocess_env_restores_the_original_when_frozen(monkeypatch) -> None:
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxxxx")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/some-real-system-path")

    env = external_subprocess_env()

    assert env is not None
    assert env["LD_LIBRARY_PATH"] == "/usr/lib/some-real-system-path"


def test_external_subprocess_env_drops_ld_library_path_when_the_original_is_itself_a_bundle_dir(monkeypatch) -> None:
    """The real incident: roastmesh-gui (frozen) launches the roastmesh CLI
    (also frozen, via gui/runner.py's roastmesh_argv) as a child process.
    The child inherits the GUI's own bundled LD_LIBRARY_PATH as its
    starting value, and PyInstaller's bootloader then records *that* as
    the child's own LD_LIBRARY_PATH_ORIG before overwriting it with the
    child's own extraction dir. "Restore ORIG" in that case puts back the
    GUI's bundle dir, not a genuinely clean value -- confirmed directly:
    `roastmesh background status` run this way crashed systemctl with
    "version `OPENSSL_3.4.0' not found", the exact bug this module exists
    to prevent, because the restored value was still a `/tmp/_MEI...` path."""
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI222222yyyyyy")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/tmp/_MEI111111xxxxxx")

    env = external_subprocess_env()

    assert env is not None
    assert "LD_LIBRARY_PATH" not in env
