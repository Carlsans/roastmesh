"""Unit tests for the GUI's "open with the OS / Artisan" helpers.

Pure subprocess logic -- only `import tkinter` is needed (not a live
`Tk()` root, which is what actually requires $DISPLAY), so these run
without Xvfb.

The prompting incident: the original version of _open_with_default_app
silently swallowed any failure (bare `except OSError: pass`), so a real
user reported "open original file does not seem to work" with zero
feedback to debug from -- and on a machine with no application registered
for .alog, xdg-open genuinely does fail (or, worse, "succeeds" by opening
a text editor instead of Artisan, confirmed during development). These
tests are about the fix: every outcome (success, launched-but-wrong-app
avoided via Artisan preference, or failure) must be reported, never silent.
"""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("tkinter")

from roastnet.gui.app import _open_alog_file, _open_with_default_app, _run_opener


def test_run_opener_reports_success_for_a_command_that_exits_zero() -> None:
    assert _run_opener(["true"]) is None


def test_run_opener_reports_the_error_for_a_command_that_exits_nonzero() -> None:
    error = _run_opener(["sh", "-c", "echo boom >&2; exit 7"])
    assert error is not None
    assert "boom" in error


def test_run_opener_reports_success_for_a_still_running_command() -> None:
    # Stands in for a real opener handing off to a long-running app --
    # this must NOT be reported as a failure just because it hasn't
    # exited yet.
    assert _run_opener(["sleep", "5"]) is None


def test_run_opener_reports_an_error_when_the_command_does_not_exist() -> None:
    error = _run_opener(["roastnet-definitely-not-a-real-command-xyz"])
    assert error is not None
    assert "could not run" in error


@pytest.mark.skipif(sys.platform != "linux", reason="this path is Linux-specific (xdg-open)")
def test_open_with_default_app_reports_failure_when_xdg_open_is_unresolvable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir -- no xdg-open on PATH
    error = _open_with_default_app(str(tmp_path / "whatever.alog"))
    assert error is not None
    assert "xdg-open" in error


@pytest.mark.skipif(sys.platform != "linux", reason="this path is Linux-specific")
def test_open_with_default_app_never_prefers_artisan(monkeypatch, tmp_path) -> None:
    fake_artisan = tmp_path / "artisan"
    fake_artisan.write_text("#!/bin/sh\nexit 0\n")
    fake_artisan.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    # If this considered Artisan (like _open_alog_file does), it would
    # succeed via the fake binary above; getting the xdg-open failure
    # instead proves a plain folder/file open never goes near Artisan.
    error = _open_with_default_app(str(tmp_path))
    assert error is not None
    assert "xdg-open" in error


@pytest.mark.skipif(sys.platform != "linux", reason="this path is Linux-specific")
def test_open_alog_file_falls_back_to_default_app_when_artisan_is_not_installed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no artisan, no xdg-open on PATH either
    error = _open_alog_file(str(tmp_path / "roast.alog"))
    assert error is not None
    assert "xdg-open" in error


@pytest.mark.skipif(sys.platform != "linux", reason="this path is Linux-specific")
def test_open_alog_file_prefers_a_real_artisan_install_over_the_os_default(monkeypatch, tmp_path) -> None:
    fake_artisan = tmp_path / "artisan"
    fake_artisan.write_text("#!/bin/sh\nexit 0\n")
    fake_artisan.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    # xdg-open isn't on PATH -- if Artisan weren't found first, this would
    # report the missing xdg-open instead of succeeding.
    error = _open_alog_file(str(tmp_path / "roast.alog"))
    assert error is None
