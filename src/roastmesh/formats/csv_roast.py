"""Tolerant CSV roast logs -- Artisan's CSV export and generic time/temp files.

Almost every roasting tool can export a CSV of time + bean/environment
temperature + event annotations. This maps such a file into an Artisan-shaped
dict by fuzzy-matching column headers, so the curve, milestones, phases, RoR and
stats all render. It deliberately records what it can and drops what it cannot
(ARCHITECTURE.md's "record what can't be interpreted rather than rejecting").
"""
from __future__ import annotations

import csv
import io

from roastmesh.formats._util import decode_text

FORMAT_NAME = "csv"

# A real .alog/JSON must never be read as CSV; those adapters run first, but as a
# second guard we require a recognizable header with both a time and a temp
# column before claiming a file.
_MAX_ROWS = 500_000  # bounded so a hostile file can't exhaust memory (quota caps size too)

# Artisan/roast temperatures: a real roast never exceeds ~300C, but 400+ is
# ordinary in Fahrenheit. Used to auto-detect the unit when the file doesn't say.
_FAHRENHEIT_HINT_C = 300.0

# Event label -> timeindex slot (order: CHARGE, DRY_END, FC_START, FC_END,
# SC_START, SC_END, DROP, COOL_END). Checked most-specific first.
_EVENT_SLOTS = [
    (("charge", "chg"), 0),
    (("dry end", "dry_end", "drye", "yellowing", "yellow", "dry"), 1),
    (("fc end", "fc_end", "fce", "first crack end"), 3),
    (("fc start", "fc_start", "fcs", "first crack start", "first crack", "1c", "fc"), 2),
    (("sc end", "sc_end", "sce", "second crack end"), 5),
    (("sc start", "sc_start", "scs", "second crack start", "second crack", "2c", "sc"), 4),
    (("cool end", "cool_end", "cool", "ce"), 7),
    (("drop", "end"), 6),
]


def _norm(s: str) -> str:
    return (s or "").strip().strip('"').lower()


def _event_slot(label: str) -> int | None:
    l = _norm(label)
    if not l:
        return None
    for needles, slot in _EVENT_SLOTS:
        if any(n in l for n in needles):
            return slot
    return None


def _parse_time(cell: str) -> float | None:
    """Seconds, from `ss(.s)`, `mm:ss`, or `h:mm:ss`. Accepts a leading '-'
    (Artisan marks pre-CHARGE time negative)."""
    c = (cell or "").strip().strip('"')
    if not c:
        return None
    sign = 1.0
    if c[0] in "+-":
        sign = -1.0 if c[0] == "-" else 1.0
        c = c[1:]
    if ":" in c:
        parts = c.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        secs = 0.0
        for n in nums:
            secs = secs * 60.0 + n
        return sign * secs
    try:
        return sign * float(c)
    except ValueError:
        return None


def _to_float(cell: str) -> float | None:
    c = (cell or "").strip().strip('"')
    if not c:
        return None
    try:
        return float(c)
    except ValueError:
        return None


def _find_header(rows: list[list[str]]) -> tuple[int, dict] | None:
    """Locate the header row and map roles -> column index. A header must yield
    at least a time column and one temperature column, or this isn't a roast CSV
    we can use."""
    for i, row in enumerate(rows[:20]):  # header is near the top
        cols = {}
        for j, cell in enumerate(row):
            name = _norm(cell)
            if not name:
                continue
            if "time" in name or name in ("t", "seconds", "sec", "elapsed"):
                cols.setdefault("time", j)
                # prefer an explicit "time1"/elapsed column over a second time col
                if name in ("time1", "time", "elapsed"):
                    cols["time"] = j
            elif "bt" in name or "bean" in name:
                cols["bt"] = j
            elif name == "et" or "environ" in name or "exhaust" in name or name.startswith("et"):
                cols["et"] = j
            elif "event" in name or "annotation" in name or name == "notes":
                cols["event"] = j
        if "time" in cols and ("bt" in cols or "et" in cols):
            return i, cols
    return None


def _detect_mode(text_head: str, temps: list[float | None]) -> str:
    low = text_head.lower()
    if "unit:f" in low or "unit=f" in low or "fahrenheit" in low or "(f)" in low or "°f" in low:
        return "F"
    if "unit:c" in low or "unit=c" in low or "celsius" in low or "(c)" in low or "°c" in low:
        return "C"
    real = [t for t in temps if t is not None]
    if real and max(real) > _FAHRENHEIT_HINT_C:
        return "F"  # 400+ is a roast in Fahrenheit; impossible in Celsius
    return "C"


def parse(raw_bytes: bytes) -> dict | None:
    text = decode_text(raw_bytes)
    if not text.strip():
        return None
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[list[str]] = []
    for row in reader:
        rows.append(row)
        if len(rows) > _MAX_ROWS:
            return None  # implausibly large -- refuse rather than materialize
    if not rows:
        return None

    found = _find_header(rows)
    if found is None:
        return None
    header_i, cols = found

    timex: list[float] = []
    temp1: list[float | None] = []  # ET
    temp2: list[float | None] = []  # BT
    timeindex = [0] * 8
    for row in rows[header_i + 1:]:
        if not any(cell.strip() for cell in row):
            continue
        t = _parse_time(row[cols["time"]]) if cols["time"] < len(row) else None
        if t is None:
            continue
        idx = len(timex)
        timex.append(t)
        temp2.append(_to_float(row[cols["bt"]]) if "bt" in cols and cols["bt"] < len(row) else None)
        temp1.append(_to_float(row[cols["et"]]) if "et" in cols and cols["et"] < len(row) else None)
        if "event" in cols and cols["event"] < len(row):
            slot = _event_slot(row[cols["event"]])
            if slot is not None and timeindex[slot] == 0:
                timeindex[slot] = idx

    if not timex:
        return None

    mode = _detect_mode("\n".join(",".join(r) for r in rows[:header_i + 1]), temp2 + temp1)
    return {
        "timex": timex,
        "temp1": temp1,
        "temp2": temp2,
        "timeindex": timeindex,
        "mode": mode,
    }
