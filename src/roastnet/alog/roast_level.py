"""Roast-level (light/city/full city/.../french) determination.

Purely from peak bean temperature (roastnet.alog.machine.ROAST_LEVEL_BANDS_C)
-- deliberately the only source. An earlier version of this also trusted an
explicit level token typed into a file's own notes/filename (e.g. "Roast:
Full City+") ahead of temperature, on the reasoning that a roaster's own
stated assessment should outrank a generic heuristic. In practice that
produced results a person had no way to make sense of -- a roast that
peaked at 196C (a light-roast temperature on any standard chart) showing
"full city+" because that's what an old note said, with nothing in the UI
explaining why -- so peak temperature now always wins, full stop, even
though a written note is sometimes more accurate for a specific machine.
"""
from __future__ import annotations

from roastnet.alog.machine import ROAST_LEVEL_BANDS_C


def classify_roast_level(
    peak_bt_c: float | None,
    bands: list[tuple[float, str]] | None = None,
) -> str | None:
    if peak_bt_c is None:
        return None
    for max_temp_c, label in (bands or ROAST_LEVEL_BANDS_C):
        if peak_bt_c <= max_temp_c:
            return label
    return None
