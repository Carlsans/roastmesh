"""Roast-level (light/city/full city/.../french) determination.

Two sources, in precedence order (most trustworthy first):
1. `guess_roast_type_from_text()` -- a level token embedded directly in a
   filename or beans_text (e.g. "Veracruz Finca La Laja - FC+ - 211C.alog").
   This is per-roast evidence, stronger than any blanket default.
2. `classify_roast_level()` -- heuristic fallback from DROP temperature, for
   files with neither an explicit tag nor a filename hint. Clearly the
   weakest signal; see machine.ROAST_LEVEL_BANDS_C.
"""
from __future__ import annotations

import re

from roastnet.alog.machine import ROAST_LEVEL_BANDS_C

# Ordered most-specific-first so "full city+" matches before the plainer
# "full city"/"fc" patterns get a chance to. Word-boundary-anchored to
# avoid false positives (e.g. bare "c" or "fc" inside an unrelated word).
_ROAST_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bfull\s*city\s*\+|\bfc\s*\+", re.IGNORECASE), "full city+"),
    (re.compile(r"\bfull\s*city\b|\bfc\b(?!\s*\+)", re.IGNORECASE), "full city"),
    (re.compile(r"\bcity\s*\+|\bc\s*\+", re.IGNORECASE), "city+"),
    (re.compile(r"\bcity\b", re.IGNORECASE), "city"),
    (re.compile(r"\bvienna\b", re.IGNORECASE), "vienna"),
    (re.compile(r"\bfrench\b", re.IGNORECASE), "french"),
    (re.compile(r"\bcinnamon\b", re.IGNORECASE), "cinnamon"),
    (re.compile(r"\blight\s*roast\b|\blight\b", re.IGNORECASE), "light"),
]


def guess_roast_type_from_text(*texts: str | None) -> str | None:
    """Scan filename/beans_text for an explicit roast-level token."""
    combined = " ".join(t for t in texts if t)
    if not combined:
        return None
    for pattern, label in _ROAST_TYPE_PATTERNS:
        if pattern.search(combined):
            return label
    return None


def classify_roast_level(
    drop_bt_c: float | None,
    bands: list[tuple[float, str]] | None = None,
) -> str | None:
    if drop_bt_c is None:
        return None
    for max_temp_c, label in (bands or ROAST_LEVEL_BANDS_C):
        if drop_bt_c <= max_temp_c:
            return label
    return None
