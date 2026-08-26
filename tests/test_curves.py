"""alog/curves.py: pure chart-derivation logic (charge offset, phase
segments, RoR, slider/extra-channel decoding). No display needed -- these
operate on plain dicts/lists, the exact shape RoastChart receives via
RoastRecord.to_dict() / `roastnet show --json`."""
from __future__ import annotations

from pathlib import Path

import pytest

from roastnet.alog.curves import (
    charge_offset_s,
    compute_ror,
    format_mmss,
    named_extra_channel,
    phase_segments,
    slider_series,
    value_at,
)
from roastnet.alog.parser import SourceMeta, parse_alog
from roastnet.alog.record import to_roast_record

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SOURCE = SourceMeta(source_type="local", source_ref="test")


def _load(name: str):
    raw = parse_alog(FIXTURES_DIR / name)
    record = to_roast_record(raw, SOURCE)
    milestones = [dict(m.__dict__) for m in record.milestones]
    return raw, record, milestones


def test_charge_offset_uses_timeindex_slot_zero() -> None:
    raw, record, _ = _load("kaleido_1.alog")
    offset = charge_offset_s(raw, record.timex_s)
    assert offset == record.timex_s[raw["timeindex"][0]]


def test_charge_offset_degrades_to_zero_for_empty_profile() -> None:
    # alexzhu_1 has an empty timex and timeindex[0] == -1 -- must not raise.
    raw, record, _ = _load("alexzhu_1.alog")
    assert record.timex_s == []
    assert charge_offset_s(raw, record.timex_s) == 0.0


def test_format_mmss_floors_rather_than_rounds() -> None:
    # 313.5 is a real milestone duration (see the reference-roast test
    # below) -- Python's round(313.5) is banker's-rounding-to-even and
    # gives 314 -> "5:14", which is wrong; Artisan's own display floors.
    assert format_mmss(313.5) == "5:13"
    assert format_mmss(168.0) == "2:48"
    assert format_mmss(0) == "0:00"


def test_phase_segments_percentages_sum_to_100() -> None:
    _, _, milestones = _load("kaleido_1.alog")
    segments = phase_segments(milestones)
    total_pct = sum(s.pct for s in segments if s.pct is not None)
    assert total_pct == pytest.approx(100.0, abs=0.5)


def test_cooling_segment_has_duration_but_no_percentage_or_rise() -> None:
    # philstyle_1 has a COOL_END milestone (kaleido_1 does not). BT falls
    # during cooling, so a rise figure would be meaningless -- the
    # reference chart shows only a duration for this segment.
    _, _, milestones = _load("philstyle_1.alog")
    segments = phase_segments(milestones)
    cooling = next(s for s in segments if s.name == "Cooling")
    assert cooling.duration_s > 0
    assert cooling.pct is None
    assert cooling.rise_c is None


def test_drying_phase_rise_is_measured_from_tp_not_charge() -> None:
    # kaleido_1's CHARGE BT (175.0, the still-hot probe from the previous
    # batch) is *higher* than its DRY_END BT (156.4) -- a naive
    # CHARGE->DRY_END rise would be negative. Artisan measures the drying
    # phase's rise from TP, the bean's actual low point, instead.
    _, _, milestones = _load("kaleido_1.alog")
    by_name = {m["name"]: m for m in milestones}
    assert by_name["CHARGE"]["bt_c"] > by_name["DRY_END"]["bt_c"]

    segments = phase_segments(milestones)
    drying = next(s for s in segments if s.name == "Drying")
    assert drying.rise_c is not None
    assert drying.rise_c > 0


def test_drying_phase_rise_falls_back_to_charge_when_tp_is_absent() -> None:
    _, _, milestones = _load("philstyle_1.alog")
    by_name = {m["name"]: m for m in milestones}
    assert "TP" not in by_name

    segments = phase_segments(milestones)
    drying = next(s for s in segments if s.name == "Drying")
    expected = by_name["DRY_END"]["bt_c"] - by_name["CHARGE"]["bt_c"]
    assert drying.rise_c == pytest.approx(expected)


def test_phase_segments_empty_for_a_profile_with_no_milestones() -> None:
    _, _, milestones = _load("alexzhu_1.alog")
    assert phase_segments(milestones) == []


def test_slider_series_decodes_to_a_percent_range_and_is_time_ordered() -> None:
    raw, record, _ = _load("kaleido_1.alog")
    offset = charge_offset_s(raw, record.timex_s)
    series = slider_series(raw, offset)
    assert "Burner" in series
    for points in series.values():
        assert all(0.0 <= v <= 100.0 for _t, v in points)
        times = [t for t, _v in points]
        assert times == sorted(times)


def test_slider_series_empty_when_no_specialevents() -> None:
    raw, record, _ = _load("philstyle_1.alog")
    offset = charge_offset_s(raw, record.timex_s)
    assert slider_series(raw, offset) == {}


def test_value_at_holds_previous_value_between_events() -> None:
    points = [(0.0, 20.0), (10.0, 50.0), (30.0, 80.0)]
    assert value_at(points, -1.0) is None
    assert value_at(points, 0.0) == 20.0
    assert value_at(points, 5.0) == 20.0
    assert value_at(points, 10.0) == 50.0
    assert value_at(points, 100.0) == 80.0
    assert value_at([], 5.0) is None


def test_named_extra_channel_finds_sv_and_converts_to_celsius() -> None:
    raw, record, _ = _load("kaleido_1.alog")
    offset = charge_offset_s(raw, record.timex_s)
    result = named_extra_channel(raw, "SV", offset)
    assert result is not None
    times, values = result
    assert len(times) == len(values) > 0
    # kaleido fixtures are already Celsius (mode 'C') -- SV should be a
    # plausible roast-setpoint temperature, not e.g. a raw percent.
    assert all(50 <= v <= 260 for v in values)


def test_named_extra_channel_returns_none_when_absent() -> None:
    raw, record, _ = _load("philstyle_1.alog")
    offset = charge_offset_s(raw, record.timex_s)
    assert named_extra_channel(raw, "SV", offset) is None


def test_compute_ror_none_for_first_sample_and_plausible_mid_roast() -> None:
    raw, record, _ = _load("kaleido_1.alog")
    offset = charge_offset_s(raw, record.timex_s)
    times = [t - offset for t in record.timex_s]
    ror = compute_ror(times, record.bt_c)
    assert ror[0] is None
    mid = len(ror) // 2
    plausible = [v for v in ror[mid - 5:mid + 5] if v is not None]
    assert plausible
    assert all(-50 < v < 100 for v in plausible)


def test_compute_ror_tolerates_short_and_empty_arrays() -> None:
    assert compute_ror([], []) == []
    assert compute_ror([0.0], [100.0]) == [None]
