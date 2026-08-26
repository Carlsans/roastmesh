"""Celsius<->Fahrenheit conversion for display only.

Every roast is parsed, computed, stored, and searched in Celsius throughout
this project (see models.temp_to_celsius) -- this module only changes what
a user sees on screen, driven by the Settings tab's Celsius/Fahrenheit
choice. Never used to reinterpret stored data, search filters, or anything
that crosses a process boundary (the CLI, the index, a peer).
"""
from __future__ import annotations

CELSIUS = "C"
FAHRENHEIT = "F"


def convert_temp(value_c: float | None, unit: str) -> float | None:
    """`value_c` (already Celsius) converted to `unit` for display.

    A plain subtraction of two values already run through this is still a
    correct temperature *difference* in the target unit -- the +32 offset
    cancels out (F1 - F2 == 1.8 * (C1 - C2)) -- so a caller computing a
    rise or a rate (gui/chart.py's phase-segment rise, rate of rise) from
    already-converted readings needs no separate delta-conversion
    function; converting the raw BT/ET readings once, up front, is enough.
    """
    if value_c is None:
        return None
    return value_c if unit != FAHRENHEIT else value_c * 9.0 / 5.0 + 32.0
