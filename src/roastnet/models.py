from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

# Order matches the confirmed .alog `timeindex` slot order: CHARGE, DRY_END,
# FC_START, FC_END, SC_START, SC_END, DROP, COOL_END. TP is not one of these
# 8 slots -- Artisan computes it separately (turning point detection) and
# it's inserted right after CHARGE in roast chronology.
TIMEINDEX_SLOTS = ["CHARGE", "DRY_END", "FC_START", "FC_END", "SC_START", "SC_END", "DROP", "COOL_END"]
MILESTONE_ORDER = ["CHARGE", "TP", "DRY_END", "FC_START", "FC_END", "SC_START", "SC_END", "DROP", "COOL_END"]

_WEIGHT_TO_GRAMS = {"g": 1.0, "kg": 1000.0, "oz": 28.349523125, "lb": 453.59237}
_VOLUME_TO_LITERS = {"l": 1.0, "ml": 0.001, "gal": 3.785411784}


def weight_to_grams(value: float | None, unit: str | None) -> float | None:
    if value is None or not value:
        return None
    factor = _WEIGHT_TO_GRAMS.get((unit or "g").strip().lower(), 1.0)
    return float(value) * factor


def temp_to_celsius(value: float | None, mode: str | None) -> float | None:
    """.alog files record their own temperature unit in the top-level `mode`
    field ('F' or 'C') -- some exports (e.g. Hottop) are Fahrenheit and
    others (e.g. Kaleido) are Celsius. Every stored/aggregated temperature
    must go through this so cross-record comparison isn't silently mixing
    units. Unknown/missing mode is assumed already-Celsius (the more common
    default) rather than guessed from magnitude."""
    if value is None:
        return None
    if (mode or "").strip().upper() == "F":
        return (value - 32.0) * 5.0 / 9.0
    return value


def density_to_g_per_l(value: float | None, weight_unit: str | None,
                        count: float | None, volume_unit: str | None) -> float | None:
    """.alog's `density`/`density_roasted` fields are [value, weight_unit,
    count, volume_unit], meaning `value` `weight_unit` per `count`
    `volume_unit` (e.g. [340.0, 'g', 1, 'l'] = 340 g/L). A value of 0 means
    density wasn't recorded for that roast (observed in every fixture on
    hand) and is treated as absent, not a real zero density."""
    if not value:
        return None
    weight_factor = _WEIGHT_TO_GRAMS.get((weight_unit or "g").strip().lower(), 1.0)
    volume_factor = _VOLUME_TO_LITERS.get((volume_unit or "l").strip().lower(), 1.0)
    count = count or 1.0
    liters = float(count) * volume_factor
    if liters <= 0:
        return None
    return (float(value) * weight_factor) / liters


@dataclass
class Milestone:
    name: str  # one of MILESTONE_ORDER
    time_s: float | None  # seconds relative to CHARGE (CHARGE itself = 0.0)
    bt_c: float | None
    et_c: float | None


@dataclass
class RoastRecord:
    # identity / provenance
    roast_id: str
    source_type: str  # "local" | "p2p"
    source_ref: str
    source_url: str | None
    fetched_at: datetime
    roast_uuid: str | None

    # machine / batch
    roaster_type_raw: str | None
    machine_key: str
    mechanism_family: str
    batch_weight_in_g: float | None
    batch_weight_out_g: float | None
    density_g_per_l: float | None

    # bean / roast metadata
    title: str | None  # Artisan's own `title` field -- often left at its default ("Roaster Scope")
    beans_text: str | None
    roast_date: str | None
    roast_epoch: int | None
    roast_type: str | None  # e.g. "full city" -- always from peak bean temperature, see roast_level.py

    # raw curves (kept for display/export; not persisted as first-class columns)
    timex_s: list[float]
    bt_c: list[float]
    et_c: list[float]

    # decoded milestones + normalized representation
    milestones: list[Milestone]
    phase_profile: dict[str, float] | None

    # free text / mined tags
    roasting_notes: str | None
    cupping_notes: str | None
    note_tags: list[str]

    is_user_log: bool
    parse_warnings: list[str]
    extra_raw: dict[str, Any] = field(default_factory=dict)

    def milestone(self, name: str) -> Milestone | None:
        for m in self.milestones:
            if m.name == name:
                return m
        return None

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["fetched_at"] = self.fetched_at.isoformat()
        d["milestones"] = [m.__dict__ for m in self.milestones]
        return d

    @staticmethod
    def new_roast_id() -> str:
        return str(uuid4())

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
