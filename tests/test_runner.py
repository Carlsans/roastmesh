"""Tests for gui/runner.py's pure helpers.

Deliberately not in test_gui.py: everything there is gated behind a display
being available, and these need no Tk at all.
"""
from __future__ import annotations

import json
import queue
import sys
import time

import pytest

from roastmesh.gui.runner import Task, describe, parse_json_output


def test_parses_ordinary_json_output() -> None:
    assert parse_json_output('{"a": 1}') == {"a": 1}
    assert parse_json_output('[1, 2, 3]') == [1, 2, 3]


def test_ignores_a_notice_printed_before_the_payload() -> None:
    """The real regression: on a brand-new install the command that happens
    to create the identity prints a notice first, and Task merges stderr into
    stdout, so `profile show --json` handed the GUI

        created new identity: <hex>
        run `roastmesh identity export` to back up ...
        {"v": 1, ...}

    json.loads() raised on that, the handler silently returned, and the
    Settings name field stayed blank forever for exactly the users seeing the
    app for the first time.
    """
    noisy = (
        "created new identity: " + "cd" * 32 + "\n"
        "run `roastmesh identity export` to back up your secret key -- "
        "it cannot be recovered if lost.\n"
        '{"v": 1, "name": "Marzipan Palate"}'
    )
    assert parse_json_output(noisy) == {"v": 1, "name": "Marzipan Palate"}


def test_handles_a_multi_line_payload_after_a_notice() -> None:
    text = 'some notice\n[\n  {"roast_id": "abc"},\n  {"roast_id": "def"}\n]'
    assert parse_json_output(text) == [{"roast_id": "abc"}, {"roast_id": "def"}]


def test_still_raises_when_there_is_no_json_at_all() -> None:
    """Callers already handle JSONDecodeError; tolerating notices must not
    turn a genuinely failed command into silence."""
    with pytest.raises(json.JSONDecodeError):
        parse_json_output("error: no such command 'wat'")


def test_describe_quotes_arguments_containing_spaces() -> None:
    assert describe(["roastmesh", "search", "washed ethiopian"]) == \
        'roastmesh search "washed ethiopian"'


def test_send_line_reaches_an_input_reading_child() -> None:
    """The Devices tab's pairing modal drives `roastmesh device pair
    --json`'s prompts this exact way -- a real subprocess actually blocked
    on input(), fed a line the same way a person typing at a terminal
    would, proving stdin=subprocess.PIPE plus send_line's write+flush
    actually reaches it."""
    task = Task(argv=[
        sys.executable, "-c",
        "line = input(); print('GOT:' + line, flush=True)",
    ])
    task.start()
    # Give the child a moment to actually start and block on input() --
    # send_line before that would just be racing the process's own startup.
    time.sleep(0.3)
    task.send_line("hello")

    lines: list[str] = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            kind, payload = task.output.get(timeout=0.2)
        except queue.Empty:
            continue
        if kind == "line":
            lines.append(payload)
        elif kind == "done":
            break

    assert "GOT:hello" in lines


def test_send_line_is_a_no_op_before_the_process_has_started() -> None:
    task = Task(argv=[sys.executable, "-c", "pass"])
    task.send_line("hello")  # must not raise -- no process exists yet


def test_send_line_is_a_no_op_after_the_process_has_exited() -> None:
    task = Task(argv=[sys.executable, "-c", "pass"])
    task.start()
    deadline = time.monotonic() + 5
    while task.running and time.monotonic() < deadline:
        time.sleep(0.02)
    task.send_line("hello")  # must not raise -- the pipe is long gone
