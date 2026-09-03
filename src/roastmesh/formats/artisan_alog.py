"""Artisan `.alog` (Python-dict repr) and Artisan's JSON export.

The `.alog` path just reuses `alog.parser.parse_alog_text` -- this adapter
exists so the existing format is one entry in the registry like any other, and
so Artisan's *JSON* export (identical field names, just JSON-encoded) is picked
up for free.
"""
from __future__ import annotations

import ast
import json

from roastmesh.formats._util import decode_text

FORMAT_NAME = "artisan"

# Keys that identify an Artisan-shaped dict, so this adapter doesn't swallow an
# arbitrary JSON object (e.g. a RoasTime export) that happens to be a dict. Any
# real Artisan profile carries the curve array and its declared unit.
_SIGNATURE_KEYS = ("timex", "temp2", "mode", "roastertype", "timeindex")


def _looks_artisan(obj: object) -> bool:
    return isinstance(obj, dict) and any(k in obj for k in _SIGNATURE_KEYS)


def parse(raw_bytes: bytes) -> dict | None:
    text = decode_text(raw_bytes)

    # Artisan JSON export first: json.loads fails on a Python-repr .alog
    # (single quotes, True/False/None), so a success here means real JSON. Claim
    # it only if it looks Artisan-shaped; a valid-but-non-Artisan JSON object is
    # left for the JSON adapters that follow (return None, don't fall through to
    # literal_eval, which could misparse it).
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if obj is not None:
        return obj if _looks_artisan(obj) else None

    # Python-repr `.alog`: literal_eval only (never eval), same as alog.parser.
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    return value if _looks_artisan(value) else None
