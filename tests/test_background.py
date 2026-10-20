from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from roastmesh import background
from roastmesh.background import Settings, node_serve_args
from roastmesh.gui import single_instance


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _write_config(**data) -> None:
    path = background.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# -- settings -> flags ------------------------------------------------------

def test_settings_default_when_the_gui_never_ran() -> None:
    s = background.load_settings()
    assert s.wan_discovery is True and s.device_sync is True
    assert s.public_port == "" and s.devices_dir == ""
    assert s.db_path and s.watch_dir


def test_settings_read_what_the_gui_saved() -> None:
    _write_config(db_path="/x/db", watch_dir="/x/w", wan_discovery_enabled=False,
                  public_port="26513", enable_device_sync=False, devices_dir="/x/d")
    assert background.load_settings() == Settings("/x/db", "/x/w", False, "26513", False, "/x/d")


def test_settings_survive_a_corrupt_or_non_object_config() -> None:
    background.settings_path().parent.mkdir(parents=True, exist_ok=True)
    for junk in ("{not json", "[1, 2]", ""):
        background.settings_path().write_text(junk, encoding="utf-8")
        assert background.load_settings().wan_discovery is True


@pytest.mark.parametrize("raw,expected", [("auto", "auto"), (" AUTO ", "auto"), ("26513", "26513"),
                                          ("0", ""), ("70000", ""), ("abc", ""), (None, "")])
def test_a_bad_public_port_never_becomes_a_cli_argument(raw, expected) -> None:
    _write_config(public_port=raw)
    assert background.load_settings().public_port == expected


def test_serve_args_for_plain_settings() -> None:
    args = node_serve_args(Settings("/d.sqlite3", "/w"))
    assert args == ["--db", "/d.sqlite3", "node", "serve", "--publish-watch-dir", "/w", "--wan-discovery"]


def test_serve_args_public_port_auto_pins_nothing_else() -> None:
    args = node_serve_args(Settings("/d", "/w", public_port="auto"))
    assert args[-2:] == ["--public-port", "auto"] and "--wan-port" not in args


def test_serve_args_fixed_port_is_both_listened_on_and_published() -> None:
    args = node_serve_args(Settings("/d", "/w", public_port="26513"))
    assert args[-4:] == ["--wan-port", "26513", "--public-port", "26513"]


def test_serve_args_without_wan_ignores_the_port() -> None:
    args = node_serve_args(Settings("/d", "/w", wan_discovery=False, public_port="26513"))
    assert "--wan-discovery" not in args and "--public-port" not in args


def test_serve_args_device_sync_off_and_custom_dir() -> None:
    assert node_serve_args(Settings("/d", "/w", device_sync=False))[-1] == "--no-device-sync"
    assert node_serve_args(Settings("/d", "/w", devices_dir=" /dev ")).count("--devices-dir") == 1
    assert "--devices-dir" not in node_serve_args(Settings("/d", "/w"))


def test_serve_args_debug_goes_right_after_serve_options_start() -> None:
    assert "--debug" in node_serve_args(Settings("/d", "/w"), debug=True)
    assert "--debug" not in node_serve_args(Settings("/d", "/w"))


def test_cli_argv_runs_this_interpreter_from_source() -> None:
    assert background.cli_argv() == [sys.executable, "-m", "roastmesh.cli"]


# -- state file -------------------------------------------------------------

def test_a_dead_pid_in_the_state_file_is_not_a_running_node() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    background._write_state(proc.pid, proc.pid)
    assert background.supervisor_pid() is None
    assert background.node_is_running() is False


def test_a_live_pid_in_the_state_file_is_a_running_node() -> None:
    background._write_state(os.getpid(), os.getpid())
    assert background.supervisor_pid() == os.getpid()
    assert background.node_is_running() is True
    background._write_state(None, None)
    assert background.node_is_running() is False


def test_state_file_garbage_means_nothing_is_running() -> None:
    background.state_path().parent.mkdir(parents=True, exist_ok=True)
    background.state_path().write_text('{"node": "x"}', encoding="utf-8")
    assert background.node_is_running() is False


# -- GUI-open probe ---------------------------------------------------------

def test_port_is_held_sees_the_gui_without_raising_its_window() -> None:
    port = 41990
    assert single_instance.port_is_held(port) is False
    focused: list[int] = []
    listener = single_instance.start_focus_listener(lambda: focused.append(1), port=port)
    try:
        assert listener is not None
        for _ in range(3):
            assert single_instance.port_is_held(port) is True
        time.sleep(0.3)
        assert focused == []   # a probe is not a focus request
    finally:
        _release(listener)
    time.sleep(0.1)
    assert single_instance.port_is_held(port) is False


# -- the supervisor ---------------------------------------------------------

def _release(listener) -> None:
    """Really free the port. close() alone does not on Linux while the accept
    thread is blocked in accept() -- the kernel keeps the listening socket
    alive until that returns -- so shut it down first. A real GUI exits as a
    process, which frees the port outright; this is only for tests that
    reopen the port within one process."""
    if listener is None:
        return
    try:
        listener.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    listener.close()


class FakeNode:
    """Stands in for the node subprocess."""

    _next_pid = 900_000

    def __init__(self) -> None:
        FakeNode._next_pid += 1
        self.pid = FakeNode._next_pid
        self.exit_code: int | None = None

    def poll(self):
        return self.exit_code


@pytest.fixture
def fake_terminate(monkeypatch):
    monkeypatch.setattr(background, "_terminate", lambda proc: setattr(proc, "exit_code", -15))


def _supervise(port: int, spawn, *, seconds: float, during=None) -> int:
    stop = threading.Event()
    result: list[int] = []
    t = threading.Thread(target=lambda: result.append(
        background.run_forever(poll=0.02, gui_port=port, stop=stop, spawn=spawn)))
    t.start()
    try:
        if during:
            during()
        time.sleep(seconds)
    finally:
        stop.set()
        t.join(5)
    assert not t.is_alive()
    return result[0]


def test_supervisor_serves_while_no_gui_is_open_and_cleans_up(fake_terminate) -> None:
    spawned: list[FakeNode] = []
    spawned_node = lambda: spawned.append(FakeNode()) or spawned[-1]
    assert _supervise(41991, spawned_node, seconds=0.3) == 0
    assert len(spawned) == 1
    assert spawned[0].exit_code == -15           # stopped on the way out
    assert background.read_state() == {}          # and the state file removed


def test_supervisor_stands_aside_while_the_gui_is_open_then_resumes(fake_terminate) -> None:
    port = 41992
    spawned: list[FakeNode] = []
    spawn = lambda: spawned.append(FakeNode()) or spawned[-1]
    listener = single_instance.start_focus_listener(lambda: None, port=port)
    assert listener is not None
    try:
        def gui_closes_later() -> None:
            time.sleep(0.3)
            assert spawned == []                  # held back the whole time the GUI was open
            _release(listener)
        _supervise(port, spawn, seconds=0.5, during=gui_closes_later)
    finally:
        _release(listener)
    assert len(spawned) == 1                      # came up once the window was gone


def test_supervisor_stops_a_running_node_when_the_gui_opens(fake_terminate) -> None:
    port = 41993
    spawned: list[FakeNode] = []
    spawn = lambda: spawned.append(FakeNode()) or spawned[-1]
    holder: list = []

    def gui_opens() -> None:
        time.sleep(0.2)
        assert len(spawned) == 1 and spawned[0].exit_code is None
        holder.append(single_instance.start_focus_listener(lambda: None, port=port))
        time.sleep(0.3)
        assert spawned[0].exit_code == -15        # handed over
        assert background.read_state().get("node") is None

    try:
        _supervise(port, spawn, seconds=0.1, during=gui_opens)
    finally:
        for listener in holder:
            _release(listener)


def test_supervisor_restarts_a_node_that_died_after_the_retry_delay(monkeypatch, fake_terminate) -> None:
    monkeypatch.setattr(background, "RESTART_DELAY_SECONDS", 0.2)
    spawned: list[FakeNode] = []
    spawn = lambda: spawned.append(FakeNode()) or spawned[-1]

    def node_crashes() -> None:
        time.sleep(0.1)
        spawned[0].exit_code = 1
        time.sleep(0.1)
        assert len(spawned) == 1                  # not respinning instantly
    _supervise(41994, spawn, seconds=0.5, during=node_crashes)
    assert len(spawned) == 2


def test_supervisor_survives_a_spawn_failure(monkeypatch, fake_terminate) -> None:
    monkeypatch.setattr(background, "RESTART_DELAY_SECONDS", 0.05)
    calls: list[int] = []

    def spawn():
        calls.append(1)
        raise OSError("no such binary")
    assert _supervise(41995, spawn, seconds=0.3) == 0
    assert len(calls) >= 2


def test_a_second_supervisor_refuses_to_start(fake_terminate) -> None:
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        background._write_state(other.pid, None)
        assert background.run_forever(poll=0.02, gui_port=41996, spawn=lambda: FakeNode()) == 1
    finally:
        other.kill()
        other.wait()
