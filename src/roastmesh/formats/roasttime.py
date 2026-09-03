"""Aillio RoasTime / roast.world JSON roast profiles.

SPECULATIVE, UNVALIDATED FIELD MAPPING -- kept deliberately (the user's call)
against the day a real file appears. roast.world blocks file download, so there
is no export to validate this against or, in practice, to feed it: a Bullet owner
who wants to share on roastmesh today logs in Artisan (which supports the Aillio
Bullet natively) and gets a real `.alog`. This adapter only earns its keep if a
*local* RoasTime file or some other Aillio export ever surfaces.

Because the exact JSON keys can't be confirmed, it is deliberately *tolerant*:
several likely spellings of each field (camelCase and snake_case), curve arrays
as plain numbers or {time,value} objects, and time explicit or from a sample
interval. Validated only against a synthesized fixture
(tests/fixtures/formats/roasttime_sample.json). The registry routes a file here
only if it carries a recognizable bean-temperature array, and this returns None
for anything else, so it cannot harm the pipeline or misclaim a non-RoasTime JSON.

SECURITY: json.loads only, never eval/pickle. Bounded array sizes.
"""
from __future__ import annotations

import json

from roastmesh.formats._util import decode_text

FORMAT_NAME = "roasttime"

_MAX_SAMPLES = 500_000
_FAHRENHEIT_HINT_C = 300.0

# Candidate key spellings, first present wins.
_BT_KEYS = ("beanTemperature", "bean_temperature", "beanTemp", "beantemp", "bt", "BT")
_ET_KEYS = ("drumTemperature", "drum_temperature", "exhaustTemperature",
            "exhaust_temperature", "environmentTemperature", "et", "ET")
_TIME_KEYS = ("time", "timex", "times", "seconds", "timeSeconds")
_NAME_KEYS = ("roastName", "name", "title")
_BEANS_KEYS = ("beanName", "beans", "beanNames", "coffeeName")
_NOTES_KEYS = ("roastNotes", "notes", "comments", "description")
_WEIGHT_IN_KEYS = ("weightGreen", "greenWeight", "weight_green", "chargeWeight")
_WEIGHT_OUT_KEYS = ("weightRoasted", "roastedWeight", "weight_roasted", "dropWeight")
_UNIT_KEYS = ("temperatureUnit", "temp_unit", "unit")
_SAMPLE_INTERVAL_KEYS = ("sampleInterval", "interval", "sampleRate", "sample_rate")

# Event -> timeindex slot. Value may be an index or a time (seconds); we detect
# which by magnitude vs. the series length. Order matches TIMEINDEX_SLOTS.
_EVENT_KEYS = {
    0: ("chargeIndex", "chargeTime", "indexCharge", "charge"),
    1: ("yellowingIndex", "yellowingTime", "dryEndIndex", "dryEndTime", "yellowing", "dryEnd"),
    2: ("firstCrackIndex", "firstCrackTime", "fcIndex", "fcTime", "firstCrack", "fcStart"),
    3: ("firstCrackEndIndex", "firstCrackEndTime", "fcEnd"),
    4: ("secondCrackIndex", "secondCrackTime", "scIndex", "scTime", "secondCrack", "scStart"),
    5: ("secondCrackEndIndex", "secondCrackEndTime", "scEnd"),
    6: ("dropIndex", "dropTime", "endIndex", "endTime", "drop"),
    7: ("coolIndex", "coolTime", "coolEnd"),
}


def _first(d: dict, keys) -> object:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _as_number_series(value) -> list[float | None] | None:
    """A curve as either [1,2,3] or [{'value':1,'time':0}, ...] -> [1,2,3]."""
    if not isinstance(value, list) or not value:
        return None
    if len(value) > _MAX_SAMPLES:
        return None
    out: list[float | None] = []
    for item in value:
        if isinstance(item, (int, float)):
            out.append(float(item))
        elif isinstance(item, dict):
            v = item.get("value", item.get("y", item.get("temp", item.get("bt"))))
            out.append(float(v) if isinstance(v, (int, float)) else None)
        else:
            out.append(None)
    return out


def _time_from_objects(value) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    times = []
    for item in value:
        if isinstance(item, dict):
            t = item.get("time", item.get("x", item.get("t")))
            if isinstance(t, (int, float)):
                times.append(float(t))
                continue
        return None
    return times if len(times) == len(value) else None


def parse(raw_bytes: bytes) -> dict | None:
    text = decode_text(raw_bytes)
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    # roast.world sometimes wraps the roast under a "roast"/"data"/"profile" key.
    if isinstance(obj, dict):
        for wrapper in ("roast", "data", "profile"):
            if isinstance(obj.get(wrapper), dict):
                obj = obj[wrapper]
                break
    if not isinstance(obj, dict):
        return None

    bt_raw = _first(obj, _BT_KEYS)
    bt = _as_number_series(bt_raw)
    if bt is None:
        return None  # no bean-temperature curve -> not a roast JSON we handle

    et = _as_number_series(_first(obj, _ET_KEYS)) or [None] * len(bt)

    # Time axis: explicit array, else from {time,...} objects, else sample interval.
    timex = None
    t_arr = _first(obj, _TIME_KEYS)
    if isinstance(t_arr, list):
        cand = _as_number_series(t_arr)
        if cand is not None and all(v is not None for v in cand):
            timex = [float(v) for v in cand]
    if timex is None:
        timex = _time_from_objects(bt_raw)
    if timex is None:
        interval = _first(obj, _SAMPLE_INTERVAL_KEYS)
        step = float(interval) if isinstance(interval, (int, float)) and interval else 1.0
        timex = [i * step for i in range(len(bt))]

    n = min(len(timex), len(bt), len(et))
    timex, bt, et = timex[:n], bt[:n], et[:n]

    timeindex = [0] * 8
    for slot, keys in _EVENT_KEYS.items():
        val = _first(obj, keys)
        if not isinstance(val, (int, float)):
            continue
        val = float(val)
        # Heuristic: a value within the series length is an index; a larger one
        # (or a float) is a time in seconds -> nearest index.
        if val == int(val) and 0 <= val < n:
            idx = int(val)
        else:
            idx = min(range(n), key=lambda i: abs(timex[i] - val)) if n else 0
        timeindex[slot] = idx

    # Unit: explicit hint, else the same >300 => Fahrenheit heuristic CSV uses.
    unit = _first(obj, _UNIT_KEYS)
    if isinstance(unit, str) and unit.strip().upper().startswith("F"):
        mode = "F"
    elif isinstance(obj.get("isFahrenheit"), bool):
        mode = "F" if obj["isFahrenheit"] else "C"
    else:
        real = [v for v in bt if v is not None]
        mode = "F" if real and max(real) > _FAHRENHEIT_HINT_C else "C"

    result: dict = {
        "timex": timex, "temp1": et, "temp2": bt, "timeindex": timeindex,
        "mode": mode,
        # roast.world/RoasTime is the Aillio Bullet's software; attribute it so
        # the machine catalogue resolves it (aillio_bullet).
        "roastertype": "Aillio Bullet",
    }
    name = _first(obj, _NAME_KEYS)
    beans = _first(obj, _BEANS_KEYS)
    notes = _first(obj, _NOTES_KEYS)
    if isinstance(name, str):
        result["title"] = name
    if isinstance(beans, str):
        result["beans"] = beans
    elif isinstance(beans, list):
        result["beans"] = ", ".join(str(b) for b in beans)
    if isinstance(notes, str):
        result["roastingnotes"] = notes
    w_in = _first(obj, _WEIGHT_IN_KEYS)
    w_out = _first(obj, _WEIGHT_OUT_KEYS)
    if isinstance(w_in, (int, float)) or isinstance(w_out, (int, float)):
        # RoasTime weights are grams.
        result["weight"] = [float(w_in) if isinstance(w_in, (int, float)) else 0.0,
                            float(w_out) if isinstance(w_out, (int, float)) else 0.0, "g"]
    return result
