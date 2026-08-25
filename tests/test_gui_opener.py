"""Unit tests for the GUI's "open with the OS / Artisan" helpers.

Pure subprocess logic -- only `import tkinter` is needed (not a live
`Tk()` root, which is what actually requires $DISPLAY), so these run
without Xvfb.

The prompting incident, part one: the original version of
_open_with_default_app silently swallowed any failure (bare `except
OSError: pass`), so a real user reported "open original file does not
seem to work" with zero feedback to debug from -- and on a machine with
no application registered for .alog, xdg-open genuinely does fail (or,
worse, "succeeds" by opening a text editor instead of Artisan, confirmed
during development). These tests are about that fix: every outcome
(success, launched-but-wrong-app avoided via Artisan preference, or
failure) must be reported, never silent.

Part two: once Artisan itself was found and launched, it still failed
with a real IOError, because its Flatpak sandbox can only see
~/Documents (confirmed via `flatpak info --show-permissions
org.artisan_scope.artisan`) -- not the actual roastnet-managed path it
was handed. _find_artisan_launcher/_stage_for_artisan are the fix, kept
general on purpose (not hardcoded to just the one packaging this was
found on) so the same class of bug doesn't resurface for some other
sandboxed distribution of Artisan later.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

from roastnet.gui.app import (
    _find_artisan_launcher,
    _is_snap_wrapper,
    _open_alog_file,
    _open_with_default_app,
    _run_opener,
    _stage_for_artisan,
)


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


# _find_artisan_launcher: how Artisan is found, and whether that launch
# method needs its file path staged first (see the staging tests further
# down). Real bug #1: Artisan installed via Flatpak (Flathub's
# org.artisan_scope.artisan) exports its PATH wrapper under that full app
# id, not under "artisan" -- shutil.which("artisan") silently misses it.

def test_find_artisan_launcher_prefers_a_plain_path_binary_and_skips_staging(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "artisan"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _find_artisan_launcher("/tmp/roast.alog") == ([str(fake)], False)


def test_find_artisan_launcher_finds_the_flatpak_exported_wrapper_and_flags_staging(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "org.artisan_scope.artisan"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _find_artisan_launcher("/tmp/roast.alog") == ([str(fake)], True)


def test_find_artisan_launcher_falls_back_to_flatpak_run_by_app_id(monkeypatch, tmp_path) -> None:
    # No artisan binary and no exported wrapper on PATH -- only the
    # `flatpak` command itself, which is enough: `flatpak run` finds an
    # installed app by id regardless of what's exported to PATH.
    fake_flatpak = tmp_path / "flatpak"
    fake_flatpak.write_text('#!/bin/sh\n[ "$1" = "info" ] && exit 0\nexit 0\n')
    fake_flatpak.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _find_artisan_launcher("/tmp/roast.alog") == (
        ["flatpak", "run", "org.artisan_scope.artisan"], True,
    )


def test_find_artisan_launcher_returns_none_when_flatpak_present_but_artisan_not_installed(monkeypatch, tmp_path) -> None:
    fake_flatpak = tmp_path / "flatpak"
    fake_flatpak.write_text("#!/bin/sh\nexit 1\n")  # `flatpak info ...` fails -- not installed
    fake_flatpak.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _find_artisan_launcher("/tmp/roast.alog") is None


def test_find_artisan_launcher_returns_none_when_nothing_is_installed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir: no artisan, no flatpak
    assert _find_artisan_launcher("/tmp/roast.alog") is None


# Defensive Snap support: not confirmed against a real Snap-packaged
# Artisan (unlike Flatpak, which was), but the same class of bug plausibly
# applies -- strict Snap confinement can restrict filesystem visibility
# too, and Snap's exported wrapper (unlike Flatpak's) keeps the plain
# command name, so it's indistinguishable from a native install by name.

def test_is_snap_wrapper_true_for_a_snap_bin_path() -> None:
    assert _is_snap_wrapper("/snap/bin/artisan") is True


def test_is_snap_wrapper_false_for_a_normal_path_binary() -> None:
    assert _is_snap_wrapper("/usr/bin/artisan") is False
    assert _is_snap_wrapper("/usr/local/bin/artisan") is False


def test_find_artisan_launcher_flags_staging_for_a_snap_style_path(monkeypatch) -> None:
    # A real /snap/bin/artisan isn't constructible under a tmp_path (Snap
    # always resolves to that literal absolute path on a real machine) --
    # shutil.which is faked directly instead, since only
    # _find_artisan_launcher's own PATH-vs-Snap-path branching is under
    # test here, not shutil.which itself.
    import roastnet.gui.app as app_module
    monkeypatch.setattr(app_module.shutil, "which",
                         lambda name: "/snap/bin/artisan" if name == "artisan" else None)
    launcher, needs_staging = _find_artisan_launcher("/tmp/roast.alog")
    assert launcher == ["/snap/bin/artisan"]
    assert needs_staging is True


# _stage_for_artisan: real bug #2, found right after fixing #1 above --
# Artisan's Flatpak sandbox can only see ~/Documents (confirmed via
# `flatpak info --show-permissions`), so handing it a raw roastnet path
# launched Artisan fine but it then failed to read the file at all (a
# real IOError, seen on a real machine).

def test_stage_for_artisan_copies_into_documents(monkeypatch, tmp_path) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    source = tmp_path / "some_roast.alog"
    source.write_text("roast data")

    staged = _stage_for_artisan(str(source))

    assert staged == str(fake_home / "Documents" / ".roastnet-open" / "roast.alog")
    assert Path(staged).read_text() == "roast data"


def test_stage_for_artisan_reuses_one_fixed_path_without_accumulating(monkeypatch, tmp_path) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))

    first_source = tmp_path / "one.alog"
    first_source.write_text("first")
    first_staged = _stage_for_artisan(str(first_source))

    second_source = tmp_path / "two.alog"
    second_source.write_text("second")
    second_staged = _stage_for_artisan(str(second_source))

    assert first_staged == second_staged
    assert Path(second_staged).read_text() == "second"
    assert len(list(Path(second_staged).parent.iterdir())) == 1


@pytest.mark.skipif(sys.platform != "linux", reason="this path is Linux-specific")
def test_open_alog_file_stages_for_a_flatpak_style_launch(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "org.artisan_scope.artisan"
    fake.write_text("#!/bin/sh\necho \"$1\" > " + str(tmp_path / "seen_path.txt") + "\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    source = tmp_path / "roast.alog"
    source.write_text("original content")

    error = _open_alog_file(str(source))

    assert error is None
    seen_path = (tmp_path / "seen_path.txt").read_text().strip()
    assert seen_path == str(fake_home / "Documents" / ".roastnet-open" / "roast.alog")


@pytest.mark.skipif(sys.platform != "linux", reason="this path is Linux-specific")
def test_open_alog_file_does_not_stage_for_a_plain_native_install(monkeypatch, tmp_path) -> None:
    # A native install has no sandbox to work around -- staging it would
    # only add a downside (edits/saves inside Artisan would silently land
    # on a disposable copy instead of the real file) with no upside.
    fake = tmp_path / "artisan"
    fake.write_text("#!/bin/sh\necho \"$1\" > " + str(tmp_path / "seen_path.txt") + "\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    source = tmp_path / "roast.alog"
    source.write_text("original content")

    error = _open_alog_file(str(source))

    assert error is None
    seen_path = (tmp_path / "seen_path.txt").read_text().strip()
    assert seen_path == str(source)
