"""gui/units.py: Celsius<->Fahrenheit conversion for display only."""
from __future__ import annotations

import pytest

from roastmesh.gui.units import CELSIUS, FAHRENHEIT, convert_temp


def test_convert_temp_celsius_passthrough() -> None:
    assert convert_temp(100.0, CELSIUS) == 100.0
    assert convert_temp(0.0, CELSIUS) == 0.0


def test_convert_temp_to_fahrenheit() -> None:
    assert convert_temp(0.0, FAHRENHEIT) == 32.0
    assert convert_temp(100.0, FAHRENHEIT) == 212.0
    assert convert_temp(-40.0, FAHRENHEIT) == -40.0  # the one point where both scales agree


def test_convert_temp_none_stays_none() -> None:
    assert convert_temp(None, CELSIUS) is None
    assert convert_temp(None, FAHRENHEIT) is None


def test_convert_temp_difference_is_correct_in_fahrenheit() -> None:
    # A rise/rate computed from two already-converted absolute readings
    # must still be a correct difference -- the +32 offset cancels out.
    # This is what lets gui/chart.py convert BT/ET once, up front, instead
    # of threading unit-awareness through RoR/phase-rise computation.
    a_c, b_c = 150.0, 196.5
    rise_c = b_c - a_c
    a_f, b_f = convert_temp(a_c, FAHRENHEIT), convert_temp(b_c, FAHRENHEIT)
    rise_f = b_f - a_f
    assert rise_f == pytest.approx(rise_c * 9.0 / 5.0)
