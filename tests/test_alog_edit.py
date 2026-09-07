"""alog/edit.py: the write-back path into a .alog file (notes editing).
No such path existed before this module -- every other reader in this
codebase only ever parses one. Round-tripping a real, unedited fixture
byte-for-byte through parse_for_edit/serialize is the load-bearing check:
Artisan itself must still be able to open whatever this module writes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from roastmesh.alog.edit import (
    AlogEditError,
    FORMAT_JSON,
    FORMAT_REPR,
    detect_format,
    parse_for_edit,
    serialize,
    set_notes,
    set_notes_in_file,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ALOG_FIXTURES = sorted(FIXTURES_DIR.glob("*.alog"))


@pytest.mark.parametrize("path", ALOG_FIXTURES, ids=[p.name for p in ALOG_FIXTURES])
def test_unedited_round_trip_is_byte_identical(path: Path) -> None:
    raw = path.read_bytes()
    data, fmt = parse_for_edit(raw)
    assert fmt == FORMAT_REPR  # every real fixture on disk today is the repr variant
    assert serialize(data, fmt) == raw


@pytest.mark.parametrize("path", ALOG_FIXTURES, ids=[p.name for p in ALOG_FIXTURES])
def test_editing_notes_changes_only_those_fields(path: Path) -> None:
    raw = path.read_bytes()
    original, _fmt = parse_for_edit(raw)

    edited_bytes = set_notes(raw, roasting_notes="a fresh roasting note", cupping_notes="a fresh cupping note")
    edited, fmt = parse_for_edit(edited_bytes)

    assert edited["roastingnotes"] == "a fresh roasting note"
    assert edited["cuppingnotes"] == "a fresh cupping note"
    # every other key is completely unchanged
    for key in original:
        if key in ("roastingnotes", "cuppingnotes"):
            continue
        assert edited[key] == original[key]
    assert set(edited) == set(original)  # no keys added or dropped


def test_set_notes_leaves_an_unspecified_field_untouched() -> None:
    raw = ALOG_FIXTURES[0].read_bytes()
    original, _fmt = parse_for_edit(raw)

    edited_bytes = set_notes(raw, roasting_notes="only this one changes")
    edited, _fmt2 = parse_for_edit(edited_bytes)

    assert edited["roastingnotes"] == "only this one changes"
    assert edited.get("cuppingnotes") == original.get("cuppingnotes")


def test_set_notes_can_blank_a_note_with_an_explicit_empty_string() -> None:
    raw = ALOG_FIXTURES[0].read_bytes()
    edited_bytes = set_notes(raw, roasting_notes="")
    edited, _fmt = parse_for_edit(edited_bytes)
    assert edited["roastingnotes"] == ""


def test_json_variant_round_trips_and_edits(tmp_path: Path) -> None:
    raw = ALOG_FIXTURES[0].read_bytes()
    data, _fmt = parse_for_edit(raw)
    json_bytes = json.dumps(data).encode("utf-8")

    assert detect_format(json_bytes) == FORMAT_JSON
    reparsed, fmt = parse_for_edit(json_bytes)
    assert fmt == FORMAT_JSON
    assert serialize(reparsed, fmt) == json.dumps(reparsed).encode("utf-8")

    edited_bytes = set_notes(json_bytes, cupping_notes="json-variant note")
    edited, edited_fmt = parse_for_edit(edited_bytes)
    assert edited_fmt == FORMAT_JSON  # editing must not silently switch variants
    assert edited["cuppingnotes"] == "json-variant note"


def test_detect_format_rejects_non_artisan_content() -> None:
    with pytest.raises(AlogEditError):
        detect_format(b'{"just": "some json", "nothing": "artisan-shaped"}')
    with pytest.raises(AlogEditError):
        detect_format(b"not parseable at all {{{")


def test_set_notes_in_file_writes_through(tmp_path: Path) -> None:
    path = tmp_path / "roast.alog"
    path.write_bytes(ALOG_FIXTURES[0].read_bytes())

    set_notes_in_file(path, roasting_notes="written to disk")

    data, _fmt = parse_for_edit(path.read_bytes())
    assert data["roastingnotes"] == "written to disk"
