"""Derive chart-ready series from a raw Artisan .alog dict plus decoded
Milestones -- the logic behind the roast curve graph in the GUI's
RoastDetailWindow (gui/chart.py), kept separate and Tk-free so it's
unit-testable against tests/fixtures/*.alog without a display, the same
split events.py already uses between raw-dict decoding and its Tk
consumers.

Real .alog files are highly ragged: a 150-file third-party sample found
`specialevents` in only 46 of them and any extra device channel in only 29,
and one bundled fixture (alexzhu_1.alog) has an empty `timex` entirely.
Every function here must degrade to an empty/None result on missing or
malformed data -- profiles arrive from strangers, so partial data is the
normal case, not the edge.

`milestones` parameters throughout are lists of plain dicts with
name/time_s/bt_c/et_c keys -- the exact shape RoastRecord.to_dict() /
`roastnet show --json` produce (see Milestone.__dict__ in models.py), not
the Milestone dataclass itself.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

from roastnet.models import plausible_temp_c, temp_to_celsius


def charge_offset_s(extra_raw: dict, timex_s: list[float]) -> float:
    """Absolute-seconds offset of CHARGE, so every curve can be plotted on
    a CHARGE-relative axis matching milestone `time_s` values. `timex_s` is
    absolute; the record doesn't otherwise carry this offset (see
    events.py's identical derivation for milestone decoding)."""
    timeindex = extra_raw.get("timeindex") or []
    if timeindex and timeindex[0] and 0 <= timeindex[0] < len(timex_s):
        return timex_s[timeindex[0]]
    return timex_s[0] if timex_s else 0.0


def format_mmss(seconds: float) -> str:
    """`mm:ss`, floored (whole elapsed seconds), not rounded -- confirmed
    against a real Artisan-rendered chart: its Drying phase is exactly
    313.5s (milestones' `computed`-dict-sourced times land on clean
    .0/.5-second values, not noisy floats), displayed as "5:13". Python's
    `round(313.5)` is banker's rounding to the nearest *even* integer and
    gives 314 -> "5:14", visibly wrong; floor matches every milestone in
    that same file exactly (TP 0:48, DRY_END 5:13, FC_END 10:33, DROP
    11:04, development phase 2:48)."""
    total = int(seconds)
    sign = "-" if total < 0 else ""
    total = abs(total)
    return f"{sign}{total // 60}:{total % 60:02d}"


@dataclass
class PhaseSegment:
    name: str  # "Drying" | "Maillard" | "Development" | "Cooling"
    t0_s: float
    t1_s: float
    duration_s: float
    pct: float | None  # % of CHARGE->DROP time; always None for Cooling
    rise_c: float | None  # BT rise across the segment; None if not computable


# (segment name, start milestone, end milestone) -- matches the standard
# drying/Maillard/development 3-phase model (phase_profile.py) plus a
# fourth Cooling segment, DROP->COOL_END, which gets a duration but no
# percentage (it isn't part of that model and has no natural denominator).
_PHASE_DEFS = [
    ("Drying", "CHARGE", "DRY_END"),
    ("Maillard", "DRY_END", "FC_START"),
    ("Development", "FC_START", "DROP"),
    ("Cooling", "DROP", "COOL_END"),
]


def phase_segments(milestones: list[dict]) -> list[PhaseSegment]:
    by_name = {m.get("name"): m for m in milestones if m.get("name")}
    charge = by_name.get("CHARGE")
    drop = by_name.get("DROP")
    total: float | None = None
    if charge and drop and charge.get("time_s") is not None and drop.get("time_s") is not None:
        candidate = drop["time_s"] - charge["time_s"]
        total = candidate if candidate and candidate > 0 else None

    tp = by_name.get("TP")

    segments: list[PhaseSegment] = []
    for name, start_name, end_name in _PHASE_DEFS:
        start = by_name.get(start_name)
        end = by_name.get(end_name)
        if start is None or end is None:
            continue
        t0, t1 = start.get("time_s"), end.get("time_s")
        if t0 is None or t1 is None or t1 <= t0:
            continue
        duration = t1 - t0
        pct = (duration / total * 100.0) if (total and name != "Cooling") else None

        rise: float | None = None
        if name == "Cooling":
            # BT falls during cooling -- a rise figure here is meaningless
            # and the reference chart shows none, only a duration.
            pass
        elif name == "Drying" and tp is not None and tp.get("time_s") is not None and tp.get("bt_c") is not None:
            # Drying's BT rise is measured from TP, not CHARGE -- BT at
            # CHARGE is often the still-hot probe from the previous batch
            # (can exceed DRY_END's BT, which would make a naive
            # CHARGE-based rise negative), while TP is the true low point
            # the bean actually rises from. Falls back to CHARGE only when
            # TP wasn't recorded.
            end_bt = end.get("bt_c")
            if end_bt is not None:
                rise = end_bt - tp["bt_c"]
        else:
            start_bt, end_bt = start.get("bt_c"), end.get("bt_c")
            if start_bt is not None and end_bt is not None:
                rise = end_bt - start_bt

        segments.append(PhaseSegment(name=name, t0_s=t0, t1_s=t1, duration_s=duration, pct=pct, rise_c=rise))

    return segments


def _smooth(values: list[float | None], window: int = 3) -> list[float | None]:
    # Only smooths positions that already have a value -- a leading sample
    # with no defined RoR (dt == 0) must stay None, not get pulled in from
    # its neighbors, so the plotted line breaks there instead of
    # extrapolating a value that was never actually computed.
    n = len(values)
    half = window // 2
    out: list[float | None] = [None] * n
    for i in range(n):
        if values[i] is None:
            continue
        chunk = [v for v in values[max(0, i - half):min(n, i + half + 1)] if v is not None]
        out[i] = sum(chunk) / len(chunk)
    return out


def compute_ror(
    times_rel: list[float], bt_c: list[float | None], window_s: float = 30.0,
) -> list[float | None]:
    """Rate of rise in °C/min, over a trailing `window_s` span, lightly
    smoothed. `None` for a leading sample with zero elapsed time in its
    window (so the plotted line breaks there instead of spiking), and for
    any sample whose window endpoint is a rejected sensor-glitch reading
    (bt_c may contain None -- see plausible_temp_c in models.py) --
    dividing by BT itself, a glitch can't leak into a computed RoR."""
    n = min(len(times_rel), len(bt_c))
    raw: list[float | None] = [None] * n
    j = 0
    for i in range(n):
        while times_rel[i] - times_rel[j] > window_s:
            j += 1
        dt = times_rel[i] - times_rel[j]
        if dt > 0 and bt_c[i] is not None and bt_c[j] is not None:
            raw[i] = (bt_c[i] - bt_c[j]) / dt * 60.0
    return _smooth(raw)


def slider_series(extra_raw: dict, charge_abs: float) -> dict[str, list[tuple[float, float]]]:
    """Decode Artisan's slider/event log (`specialevents` + friends) into
    one step-point list per control channel, keyed by its label from
    `etypes` (typically "Air"/"Drum"/"Damper"/"Burner") -- decoded
    generically by name rather than assumed index order, so any of them
    renders if present. `value` is Artisan's 0-10 slider scale; converted
    to a 0-100% convention via (value-1)*10, confirmed against real roasts
    to yield clean percentages."""
    timex = extra_raw.get("timex") or []
    events = extra_raw.get("specialevents") or []
    types = extra_raw.get("specialeventstype") or []
    values = extra_raw.get("specialeventsvalue") or []
    etypes = extra_raw.get("etypes") or []
    n = min(len(events), len(types), len(values))

    result: dict[str, list[tuple[float, float]]] = {}
    for i in range(n):
        idx = events[i]
        if idx is None or idx < 0 or idx >= len(timex):
            continue
        type_idx = types[i]
        if type_idx is None or type_idx < 0 or type_idx >= len(etypes):
            continue
        label = etypes[type_idx]
        if not label or label == "--":
            continue
        raw_value = values[i]
        if raw_value is None:
            continue
        pct = (raw_value - 1.0) * 10.0
        result.setdefault(label, []).append((timex[idx] - charge_abs, pct))

    for points in result.values():
        points.sort(key=lambda p: p[0])
    return result


def value_at(step_points: list[tuple[float, float]], t: float) -> float | None:
    """Step-hold lookup: the value in effect at time `t` -- the value set
    by the most recent event at or before `t`, or None before the first
    event (or if there are none at all)."""
    if not step_points:
        return None
    times = [p[0] for p in step_points]
    i = bisect.bisect_right(times, t) - 1
    if i < 0:
        return None
    return step_points[i][1]


def named_extra_channel(
    extra_raw: dict, name: str, charge_abs: float,
) -> tuple[list[float], list[float | None]] | None:
    """An extra device sub-curve identified by its Artisan display name
    (e.g. "SV" for a Kaleido's set-value/setpoint curve) rather than by
    channel index, since which channel index carries which name varies by
    machine. `extraname1[i]`/`extraname2[i]` name `extratemp1[i]`/
    `extratemp2[i]` respectively, paired with `extratimex[i]`. Converted to
    Celsius via the same top-level `mode` every other temperature uses, and
    the same sensor-glitch sentinel check every other temperature reading
    gets (see plausible_temp_c) -- an extra device channel is no less
    exposed to a bad reading than BT/ET are. Returns None if no channel has
    that name."""
    mode = extra_raw.get("mode")
    timex_channels = extra_raw.get("extratimex") or []
    for names_key, temps_key in (("extraname1", "extratemp1"), ("extraname2", "extratemp2")):
        names = extra_raw.get(names_key) or []
        temps = extra_raw.get(temps_key) or []
        for i, label in enumerate(names):
            if label != name or i >= len(temps) or i >= len(timex_channels):
                continue
            times = [t - charge_abs for t in timex_channels[i]]
            vals = [plausible_temp_c(temp_to_celsius(v, mode)) for v in temps[i]]
            if times and vals:
                return times, vals
    return None
