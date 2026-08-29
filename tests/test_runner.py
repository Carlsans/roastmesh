"""Tests for gui/runner.py's pure helpers.

Deliberately not in test_gui.py: everything there is gated behind a display
being available, and these need no Tk at all.
"""
from __future__ import annotations

import json

import pytest

from roastmesh.gui.runner import describe, parse_json_output


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
