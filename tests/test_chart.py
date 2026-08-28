"""gui/chart.py: RoastChart. Needs a real Tk root (Canvas item counts,
<Configure>/<Motion> events), so runs headless under Xvfb like the rest of
tests/test_gui.py and skips otherwise."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

import tkinter as tk

from roastmesh.alog.parser import SourceMeta, parse_alog
from roastmesh.alog.record import to_roast_record
from roastmesh.gui.chart import RoastChart
from roastmesh.gui.units import CELSIUS, FAHRENHEIT, convert_temp

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SOURCE = SourceMeta(source_type="local", source_ref="test")


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY")) or shutil.which("Xvfb") is not None


pytestmark = pytest.mark.skipif(not _has_display(), reason="no X display and no Xvfb")


def _record(name: str) -> dict:
    raw = parse_alog(FIXTURES_DIR / name)
    record = to_roast_record(raw, SOURCE)
    return record.to_dict()


@pytest.fixture
def root():
    r = tk.Tk()
    r.geometry("1040x820")
    yield r
    r.destroy()


def test_chart_constructs_and_draws_for_a_full_featured_profile(root: tk.Tk) -> None:
    # kaleido_1 has BT/ET, milestones, slider events, and an SV channel --
    # every optional element the chart can draw.
    chart = RoastChart(root, _record("kaleido_1.alog"))
    root.update()
    assert chart.plot.winfo_width() > 20
    assert len(chart.plot.find_all()) > 20
    assert len(chart.phase_canvas.find_all()) > 0


def test_chart_constructs_and_draws_with_only_bt_and_et(root: tk.Tk) -> None:
    # philstyle_1 has no specialevents, no extra channels -- only curves
    # and milestones. Must not raise despite every optional series missing.
    chart = RoastChart(root, _record("philstyle_1.alog"))
    root.update()
    assert len(chart.plot.find_all()) > 5


def test_chart_shows_fallback_message_for_a_profile_with_no_curve_data(root: tk.Tk) -> None:
    chart = RoastChart(root, _record("alexzhu_1.alog"))
    root.update()
    items = chart.plot.find_all()
    assert len(items) == 1
    assert "No curve data" in chart.plot.itemcget(items[0], "text")


def test_hover_updates_readout_and_leave_restores_idle_text(root: tk.Tk) -> None:
    chart = RoastChart(root, _record("kaleido_1.alog"))
    root.update()

    tr = chart._transform
    assert tr is not None
    mid_x = int((tr["body_left"] + tr["body_right"]) / 2)
    mid_y = int((tr["temp_top"] + tr["temp_bottom"]) / 2)

    event = tk.Event()
    event.x = mid_x
    event.y = mid_y
    chart._on_motion(event)
    root.update()

    line1 = chart.readout_line1.get()
    assert "BT" in line1 and "ET" in line1
    assert "Hover the chart" not in line1
    assert len(chart.plot.find_withtag("hover")) > 0

    chart._on_leave()
    root.update()
    assert chart.readout_line1.get() == "Hover the chart for a reading at that time."
    assert len(chart.plot.find_withtag("hover")) == 0


def test_hover_outside_plot_body_clears_crosshair(root: tk.Tk) -> None:
    chart = RoastChart(root, _record("kaleido_1.alog"))
    root.update()

    tr = chart._transform
    event = tk.Event()
    event.x = int((tr["body_left"] + tr["body_right"]) / 2)
    event.y = int((tr["temp_top"] + tr["temp_bottom"]) / 2)
    chart._on_motion(event)
    root.update()
    assert len(chart.plot.find_withtag("hover")) > 0

    outside = tk.Event()
    outside.x = tr["body_right"] + 100
    outside.y = event.y
    chart._on_motion(outside)
    root.update()
    assert len(chart.plot.find_withtag("hover")) == 0
    assert chart.readout_line1.get() == "Hover the chart for a reading at that time."


def test_resize_redraws_without_error(root: tk.Tk) -> None:
    chart = RoastChart(root, _record("kaleido_1.alog"))
    root.update()
    before = len(chart.plot.find_all())
    assert before > 0

    root.geometry("700x600")
    root.update()
    after = len(chart.plot.find_all())
    assert after > 0


def test_milestone_labels_convert_to_fahrenheit_when_selected(root: tk.Tk) -> None:
    record = _record("kaleido_1.alog")
    charge = next(m for m in record["milestones"] if m["name"] == "CHARGE")
    expected_f = convert_temp(charge["bt_c"], FAHRENHEIT)

    chart = RoastChart(root, record, unit=FAHRENHEIT)
    root.update()

    texts = [chart.plot.itemcget(i, "text") for i in chart.plot.find_all()
             if chart.plot.type(i) == "text"]
    assert any(f"{expected_f:.1f}°F" in t for t in texts)
    assert not any(f"{charge['bt_c']:.1f}C" in t for t in texts)


def test_hover_readout_uses_the_selected_unit(root: tk.Tk) -> None:
    chart = RoastChart(root, _record("kaleido_1.alog"), unit=FAHRENHEIT)
    root.update()
    tr = chart._transform
    event = tk.Event()
    event.x = int((tr["body_left"] + tr["body_right"]) / 2)
    event.y = int((tr["temp_top"] + tr["temp_bottom"]) / 2)
    chart._on_motion(event)
    root.update()
    line1 = chart.readout_line1.get()
    assert "BT" in line1 and "F" in line1
    assert "BT --" not in line1  # kaleido_1 has real BT data at this hover point


def test_ror_readout_scales_correctly_between_celsius_and_fahrenheit(root: tk.Tk) -> None:
    # Confirms gui/chart.py's "convert BT/ET once, up front" design (see
    # _build_series) actually produces a correct RoR in the target unit,
    # not just that units.convert_temp's own arithmetic is right in
    # isolation (already covered by tests/test_units.py).
    record = _record("kaleido_1.alog")

    chart_c = RoastChart(root, record, unit=CELSIUS)
    root.update()
    times = chart_c._series["times"]
    mid_t = times[len(times) // 2]
    tr_c = chart_c._transform
    ev_c = tk.Event()
    ev_c.x = int(tr_c["x_of"](mid_t))
    ev_c.y = int((tr_c["temp_top"] + tr_c["temp_bottom"]) / 2)
    chart_c._on_motion(ev_c)
    root.update()
    ror_c_text = chart_c.readout_line1.get().split("ΔBT ")[1].split("°C/min")[0]
    chart_c.destroy()

    chart_f = RoastChart(root, record, unit=FAHRENHEIT)
    root.update()
    tr_f = chart_f._transform
    ev_f = tk.Event()
    ev_f.x = int(tr_f["x_of"](mid_t))
    ev_f.y = int((tr_f["temp_top"] + tr_f["temp_bottom"]) / 2)
    chart_f._on_motion(ev_f)
    root.update()
    ror_f_text = chart_f.readout_line1.get().split("ΔBT ")[1].split("°F/min")[0]

    assert ror_c_text != "--" and ror_f_text != "--"  # both defined at this mid-roast point
    assert float(ror_f_text) == pytest.approx(float(ror_c_text) * 1.8, abs=0.15)
