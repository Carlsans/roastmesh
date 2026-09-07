"""Writing back into a `.alog` file -- specifically, editing the notes
fields (roastingnotes/cuppingnotes) without opening Artisan.

No write path into an `.alog` existed anywhere in this codebase before this
module: every other file here only ever reads one (alog/parser.py,
formats/artisan_alog.py). A `.alog` is not JSON -- it's the Python repr() of
a dict (single-quoted strings, capitalized True/False/None), the same
literal-eval-only format alog/parser.py already reads (see its own docstring
for why eval()/pickle are never used) -- so `repr()` is the correct, safe
counterpart for writing it back: it produces exactly the literal syntax
ast.literal_eval accepts, preserves key insertion order (dicts are
order-preserving since Python 3.7, and literal_eval's result keeps the
source's own key order), and round-trips a float back to an equal value
(possibly a different but equivalent string -- e.g. "1.0" vs "1", both
valid Python float literals -- which is fine since Artisan reads the float
*value*, not its exact source spelling).

A less common "Artisan JSON export" variant also exists (same field names,
plain JSON) -- detected and preserved the same way formats/artisan_alog.py
already detects it for reading, so an edit never silently changes which
variant a file is.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from roastmesh.formats._util import decode_text

FORMAT_REPR = "repr"
FORMAT_JSON = "json"

# Same signature keys formats/artisan_alog.py uses to recognize an
# Artisan-shaped dict, so this module's notion of "is this a real .alog"
# never drifts from the reader's.
_SIGNATURE_KEYS = ("timex", "temp2", "mode", "roastertype", "timeindex")


class AlogEditError(Exception):
    """Raised when a file can't be parsed as an editable Artisan .alog."""


def _looks_artisan(obj: object) -> bool:
    return isinstance(obj, dict) and any(k in obj for k in _SIGNATURE_KEYS)


def detect_format(raw_bytes: bytes) -> str:
    """FORMAT_JSON or FORMAT_REPR -- raises AlogEditError if neither parses
    as an Artisan-shaped dict at all (same detection order
    formats/artisan_alog.py's reader uses: JSON first, since a Python-repr
    .alog's single-quoted strings always fail json.loads outright)."""
    text = decode_text(raw_bytes)
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if obj is not None:
        if _looks_artisan(obj):
            return FORMAT_JSON
        raise AlogEditError("valid JSON, but not an Artisan-shaped profile")

    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError) as exc:
        raise AlogEditError(f"could not parse as JSON or a Python literal: {exc}") from exc
    if not _looks_artisan(value):
        raise AlogEditError("parsed, but not an Artisan-shaped profile")
    return FORMAT_REPR


def parse_for_edit(raw_bytes: bytes) -> tuple[dict, str]:
    """Parse raw .alog bytes into (dict, format) -- format is whichever of
    FORMAT_JSON/FORMAT_REPR serialize() needs to write it back the same way.
    """
    fmt = detect_format(raw_bytes)
    text = decode_text(raw_bytes)
    if fmt == FORMAT_JSON:
        return json.loads(text), fmt
    return ast.literal_eval(text), fmt


def serialize(data: dict, fmt: str) -> bytes:
    if fmt == FORMAT_JSON:
        return json.dumps(data).encode("utf-8")
    if fmt == FORMAT_REPR:
        return repr(data).encode("utf-8")
    raise ValueError(f"unknown .alog format: {fmt!r}")


def set_notes(raw_bytes: bytes, *, roasting_notes: str | None = None, cupping_notes: str | None = None) -> bytes:
    """Return new .alog bytes with only roastingnotes/cuppingnotes changed,
    in the same on-disk format the input was in. A field left as None here
    is left completely untouched in the output (not cleared) -- pass an
    empty string explicitly to actually blank a note out.
    """
    data, fmt = parse_for_edit(raw_bytes)
    if roasting_notes is not None:
        data["roastingnotes"] = roasting_notes
    if cupping_notes is not None:
        data["cuppingnotes"] = cupping_notes
    return serialize(data, fmt)


def set_notes_in_file(path: Path, *, roasting_notes: str | None = None, cupping_notes: str | None = None) -> None:
    path = Path(path)
    new_bytes = set_notes(path.read_bytes(), roasting_notes=roasting_notes, cupping_notes=cupping_notes)
    path.write_bytes(new_bytes)
