"""Decode CHARGE/TP/DRY_END/FC/SC/DROP/COOL milestones from a raw .alog dict.

Two sources, preferred in this order:

1. The `computed` dict, which Artisan itself populates with CHARGE-relative
   *_time/_BT/_ET fields (e.g. `DRY_time`, `DRY_BT`, `DRY_ET`). This is the
   most reliable source and is what recent Artisan versions always provide
   for TP (turning point), since TP is algorithmically detected and has no
   fixed slot in `timeindex`.
2. `timeindex`, a fixed 8-slot list of indices into `timex`/`temp1`/`temp2`
   in the order CHARGE, DRY_END, FC_START, FC_END, SC_START, SC_END, DROP,
   COOL_END. A slot value of 0 means that milestone wasn't marked in that
   roast. Used only to fill in a milestone the `computed` dict is missing.

`temp1` is ET (environment temp), `temp2` is BT (bean temp).
"""
from __future__ import annotations

from roastmesh.models import TIMEINDEX_SLOTS, Milestone

_TIME_KEY = {"CHARGE": "CHARGE_time", "TP": "TP_time", "DRY_END": "DRY_time",
             "FC_START": "FCs_time", "FC_END": "FCe_time", "SC_START": "SCs_time",
             "SC_END": "SCe_time", "DROP": "DROP_time", "COOL_END": "COOL_time"}
_BT_KEY = {"CHARGE": "CHARGE_BT", "TP": "TP_BT", "DRY_END": "DRY_BT",
           "FC_START": "FCs_BT", "FC_END": "FCe_BT", "SC_START": "SCs_BT",
           "SC_END": "SCe_BT", "DROP": "DROP_BT", "COOL_END": "COOL_BT"}
_ET_KEY = {"CHARGE": "CHARGE_ET", "TP": "TP_ET", "DRY_END": "DRY_ET",
           "FC_START": "FCs_ET", "FC_END": "FCe_ET", "SC_START": "SCs_ET",
           "SC_END": "SCe_ET", "DROP": "DROP_ET", "COOL_END": "COOL_ET"}


def _from_timeindex(raw: dict, name: str, charge_abs_s: float | None) -> Milestone | None:
    timeindex = raw.get("timeindex") or []
    timex = raw.get("timex") or []
    temp1 = raw.get("temp1") or []  # ET
    temp2 = raw.get("temp2") or []  # BT
    try:
        slot = TIMEINDEX_SLOTS.index(name)
    except ValueError:
        return None
    if slot >= len(timeindex):
        return None
    idx = timeindex[slot]
    if not idx or idx >= len(timex):
        return None
    time_s = timex[idx] - charge_abs_s if charge_abs_s is not None else None
    bt_c = temp2[idx] if idx < len(temp2) else None
    et_c = temp1[idx] if idx < len(temp1) else None
    return Milestone(name=name, time_s=time_s, bt_c=bt_c, et_c=et_c)


def extract_milestones(raw: dict, warnings: list[str]) -> list[Milestone]:
    computed = raw.get("computed") or {}
    timeindex = raw.get("timeindex") or []
    timex = raw.get("timex") or []

    charge_abs_s: float | None = None
    # `0 <= timeindex[0]` (not a plain `timeindex[0]` truthiness check): CHARGE
    # at index 0 is a real, common case for logs that start recording *at*
    # charge -- Artisan .alog files carry pre-charge data so their CHARGE index
    # is > 0, but CSV/RoasTime exports (formats/) routinely put CHARGE at the
    # first sample. Artisan's own unset sentinel is -1, which the range check
    # still excludes. Without this, an index-0 CHARGE was read as "unset" and
    # every other milestone's time collapsed to None.
    if timeindex and 0 <= timeindex[0] < len(timex):
        charge_abs_s = timex[timeindex[0]]

    milestones: list[Milestone] = []
    for name in ["CHARGE", "TP", "DRY_END", "FC_START", "FC_END", "SC_START", "SC_END", "DROP", "COOL_END"]:
        time_s = computed.get(_TIME_KEY[name])
        bt_c = computed.get(_BT_KEY[name])
        et_c = computed.get(_ET_KEY[name])
        if name == "CHARGE" and time_s is None:
            time_s = 0.0  # CHARGE is the reference point; Artisan omits CHARGE_time
        if name == "TP" and time_s == 0.0:
            # 0.0 is Artisan's "not computed" sentinel for TP, not a real
            # value -- a roast can't turn point instantaneously at CHARGE.
            time_s = None
            bt_c = None
            et_c = None

        if time_s is not None or bt_c is not None:
            milestones.append(Milestone(name=name, time_s=time_s, bt_c=bt_c, et_c=et_c))
            continue

        # fall back to timeindex lookup (covers files with a sparser `computed` dict)
        fallback = _from_timeindex(raw, name, charge_abs_s)
        if fallback is not None:
            milestones.append(fallback)
        elif name in ("CHARGE", "DROP"):
            warnings.append(f"milestone {name} not found via computed dict or timeindex")

    return milestones
