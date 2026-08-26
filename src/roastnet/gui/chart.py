"""Roast curve chart for RoastDetailWindow: BT/ET/RoR temperature plot,
milestone markers, phase bar, a burner/air/drum control-percentage band,
and a mouse-hover readout.

Hand-drawn on a plain tk.Canvas -- no matplotlib/numpy. Measured, not
assumed: a full redraw of a real roast (300-800 samples, from this
project's own live search index) takes 1.4-5.9ms, and a synthetic
5000-point stress case 18.8ms -- comfortably inside a resize/hover budget.
matplotlib+numpy would roughly quadruple the packaged GUI binary's size and
break ARCHITECTURE.md's "single native binary, no dependency chain" line,
for no rendering benefit at this data scale.

Font point sizes below (_TICK_FONT etc.) are deliberately left as plain
literals, not wrapped in sp() -- RoastnetApp bumps Tk's global font
scaling once at startup, which already resizes every point-sized font in
the app, Canvas text included (confirmed empirically: a 7pt Canvas label
grew from 13px to 39px linespace after a 3x scaling bump on a real 4K
display). What that scaling call does NOT touch is raw pixel geometry
(margins, tick lengths, dot radii, dash lengths) or stroke widths -- those
are scaled explicitly here via sp()/lw() so the chart stays proportional
once its text grows.
"""
from __future__ import annotations

import bisect
import tkinter as tk
from tkinter import ttk

from roastnet.alog import curves
from roastnet.gui import units
from roastnet.gui.i18n import t
from roastnet.gui.widgets import BG, FG, FONT_MONO, MUTED, lw, sp

_BT_COLOR = "#c8102e"
_ET_COLOR = "#1f5fa9"
_ROR_COLOR = "#e0607e"
_SV_COLOR = "#ff9300"
# Artisan's own default device colors, confirmed against real .alog fixtures
# (extradevicecolor1/2).
_CONTROL_COLORS = {"Burner": "#ad0427", "Air": "#48abff", "Drum": "#80531a", "Damper": "#7a4a2b"}
_CONTROL_ORDER = ("Burner", "Drum", "Air", "Damper")

_DEV_BAND = "#fdf6c3"
_COOL_BAND = "#dce9f5"
_BAND_ALT = "#eeece6"
_GRID = "#d8d5cd"

_CONTROL_FRAC = 0.22  # bottom slice of the plot body reserved for burner/air/drum -- a ratio, not a pixel size
_TICK_FONT = ("TkDefaultFont", 7)
_LABEL_FONT = ("TkDefaultFont", 7)


class RoastChart(ttk.Frame):
    """`record` is the full dict a search-result detail window already has
    in hand (RoastRecord.to_dict(), via `roastnet show --json`) -- no extra
    data fetch needed. Degrades to a plain "no curve data" message when
    `timex_s` is empty (e.g. tests/fixtures/alexzhu_1.alog)."""

    def __init__(self, parent: tk.Widget, record: dict, unit: str = units.CELSIUS) -> None:
        super().__init__(parent)
        self.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        self._unit = unit
        self._series = self._build_series(record, unit)
        self._transform: dict | None = None
        # Computed here, not as module constants, so a fresh scale (see
        # gui/widgets.py's set_scale) is picked up by every chart opened
        # after it changes -- a module-level `sp(52)` would freeze in
        # whatever scale was active the first time this file was imported,
        # for the rest of the process's life.
        self._margin_l = sp(52)
        self._margin_r = sp(46)
        self._margin_t = sp(12)
        self._margin_b = sp(26)

        self.phase_canvas = tk.Canvas(self, height=sp(46), bg=BG, highlightthickness=0)
        self.phase_canvas.pack(fill="x", pady=(4, 0))
        self.phase_canvas.bind("<Configure>", lambda _e: self._draw_phase_bar())

        self.plot = tk.Canvas(self, bg="#ffffff", highlightthickness=1,
                               highlightbackground=_GRID, height=sp(340))
        self.plot.pack(fill="both", expand=True, pady=(6, 0))
        self.plot.bind("<Configure>", lambda _e: self._redraw())
        self.plot.bind("<Motion>", self._on_motion)
        self.plot.bind("<Leave>", self._on_leave)

        readout = ttk.Frame(self)
        readout.pack(fill="x", pady=(4, 8))
        self.readout_line1 = tk.StringVar(value=t("Hover the chart for a reading at that time."))
        self.readout_line2 = tk.StringVar(value="")
        tk.Label(readout, textvariable=self.readout_line1, font=FONT_MONO, fg=FG, bg=BG,
                 anchor="w").pack(fill="x")
        tk.Label(readout, textvariable=self.readout_line2, font=FONT_MONO, fg=MUTED, bg=BG,
                 anchor="w").pack(fill="x")

        self._draw_phase_bar()
        self._redraw()

    # -- data prep --------------------------------------------------------

    @staticmethod
    def _build_series(record: dict, unit: str) -> dict:
        timex_s = record.get("timex_s") or []
        bt_c = record.get("bt_c") or []
        et_c = record.get("et_c") or []
        extra_raw = record.get("extra_raw") or {}
        milestones = record.get("milestones") or []

        charge_abs = curves.charge_offset_s(extra_raw, timex_s)
        n = min(len(timex_s), len(bt_c), len(et_c))
        times = [timex_s[i] - charge_abs for i in range(n)]
        # Converted once, up front, to the display unit -- everything
        # downstream (RoR, phase-segment rise, axis scaling, labels) then
        # operates on already-converted numbers with no further
        # unit-awareness needed. A plain subtraction of two converted
        # absolute readings is still a correct *difference* in that unit
        # (see units.convert_temp's docstring), which is what makes this
        # single upfront pass sufficient instead of threading `unit`
        # through every downstream computation.
        bt_c = [units.convert_temp(v, unit) for v in bt_c[:n]]
        et_c = [units.convert_temp(v, unit) for v in et_c[:n]]
        milestones = [
            {**m, "bt_c": units.convert_temp(m.get("bt_c"), unit),
             "et_c": units.convert_temp(m.get("et_c"), unit)}
            for m in milestones
        ]

        sv = curves.named_extra_channel(extra_raw, "SV", charge_abs) if times else None
        if sv is not None:
            sv_times, sv_vals = sv
            sv = (sv_times, [units.convert_temp(v, unit) for v in sv_vals])

        return {
            "times": times,
            "bt_c": bt_c,
            "et_c": et_c,
            "ror": curves.compute_ror(times, bt_c) if times else [],
            "controls": curves.slider_series(extra_raw, charge_abs) if times else {},
            "sv": sv,
            "segments": curves.phase_segments(milestones),
            "milestones": milestones,
        }

    # -- phase bar ----------------------------------------------------------

    _PHASE_SHADES = {"Drying": "#e7e4dc", "Maillard": "#cfcbc0",
                      "Development": _DEV_BAND, "Cooling": _COOL_BAND}

    def _draw_phase_bar(self) -> None:
        c = self.phase_canvas
        c.delete("all")
        segments = self._series["segments"]
        if not segments:
            return
        width = c.winfo_width()
        if width < 20:
            return
        total = sum(s.duration_s for s in segments) or 1.0
        x = 0.0
        for seg in segments:
            w = seg.duration_s / total * width
            c.create_rectangle(x, sp(20), x + w, sp(34), fill=self._PHASE_SHADES.get(seg.name, "#e7e4dc"),
                                outline="")
            label = curves.format_mmss(seg.duration_s)
            if seg.pct is not None:
                label += f"  {seg.pct:.1f}%"
            c.create_text(x + w / 2, sp(10), text=label, font=_LABEL_FONT, fill=FG)
            if seg.rise_c is not None:
                c.create_text(x + w / 2, sp(40), text=f"{seg.rise_c:.1f}°{self._unit}",
                              font=_LABEL_FONT, fill=MUTED)
            x += w

    # -- main plot ----------------------------------------------------------

    def _redraw(self) -> None:
        c = self.plot
        c.delete("all")
        s = self._series
        if not s["times"]:
            c.create_text(sp(16), sp(16), text=t("No curve data in this profile."), anchor="nw",
                          font=("TkDefaultFont", 10), fill=MUTED)
            self._transform = None
            return

        width, height = c.winfo_width(), c.winfo_height()
        if width < 20 or height < 20:
            return  # not laid out yet -- a later <Configure> will redraw

        body_left, body_right = self._margin_l, width - self._margin_r
        body_top, body_bottom = self._margin_t, height - self._margin_b
        if body_right <= body_left or body_bottom <= body_top:
            return

        control_h = (body_bottom - body_top) * _CONTROL_FRAC
        temp_top, temp_bottom = body_top, body_bottom - control_h

        times = s["times"]
        t_min, t_max = times[0], times[-1]
        if t_max <= t_min:
            t_max = t_min + 1.0

        temps = [v for v in list(s["bt_c"]) + list(s["et_c"]) if v is not None]
        if s["sv"] is not None:
            temps += [v for v in s["sv"][1] if v is not None]
        if not temps:
            temps = [0.0, 1.0]
        temp_lo, temp_hi = min(temps), max(temps)
        pad = max(1.0, (temp_hi - temp_lo) * 0.08)
        temp_lo, temp_hi = temp_lo - pad, temp_hi + pad

        ror_vals = [v for v in s["ror"] if v is not None]
        ror_lo, ror_hi = (min(ror_vals), max(ror_vals)) if ror_vals else (0.0, 20.0)
        if ror_hi <= ror_lo:
            ror_hi = ror_lo + 1.0

        def x_of(t: float) -> float:
            return body_left + (t - t_min) / (t_max - t_min) * (body_right - body_left)

        def y_temp(v: float) -> float:
            return temp_bottom - (v - temp_lo) / (temp_hi - temp_lo) * (temp_bottom - temp_top)

        def y_ror(v: float) -> float:
            return temp_bottom - (v - ror_lo) / (ror_hi - ror_lo) * (temp_bottom - temp_top)

        def y_ctrl(pct: float) -> float:
            return body_bottom - max(0.0, min(100.0, pct)) / 100.0 * control_h

        self._transform = {
            "x_of": x_of, "y_temp": y_temp, "t_min": t_min, "t_max": t_max,
            "body_left": body_left, "body_right": body_right,
            "temp_top": temp_top, "temp_bottom": temp_bottom,
        }

        by_name = {m.get("name"): m for m in s["milestones"] if m.get("name")}

        self._draw_bands(c, body_left, body_right, temp_top, temp_bottom)
        self._draw_phase_backgrounds(c, by_name, x_of, t_min, t_max, body_top, body_bottom)
        self._draw_axes(c, body_left, body_right, temp_top, temp_bottom, body_bottom,
                        temp_lo, temp_hi, ror_lo, ror_hi, t_min, t_max, x_of)
        self._draw_controls(c, s["controls"], x_of, y_ctrl, t_max)
        self._draw_ror(c, times, s["ror"], x_of, y_ror)
        self._draw_temp_line(c, times, s["et_c"], x_of, y_temp, _ET_COLOR, lw(1))
        if s["sv"] is not None:
            sv_times, sv_vals = s["sv"]
            self._draw_temp_line(c, sv_times, sv_vals, x_of, y_temp, _SV_COLOR, lw(1))
        self._draw_temp_line(c, times, s["bt_c"], x_of, y_temp, _BT_COLOR, lw(2))
        self._draw_milestones(c, s["milestones"], x_of, y_temp, t_min, t_max, temp_top)
        self._draw_legend(c, body_right, body_top, bool(ror_vals), s["sv"] is not None, s["controls"])

    @staticmethod
    def _draw_bands(c, left, right, top, bottom, stripes: int = 6) -> None:
        for i in range(stripes):
            if i % 2 != 0:
                continue
            y0 = top + (bottom - top) * i / stripes
            y1 = top + (bottom - top) * (i + 1) / stripes
            c.create_rectangle(left, y0, right, y1, fill=_BAND_ALT, outline="")

    @staticmethod
    def _draw_phase_backgrounds(c, by_name, x_of, t_min, t_max, top, bottom) -> None:
        def span(name_a, name_b):
            a, b = by_name.get(name_a), by_name.get(name_b)
            if not a or not b or a.get("time_s") is None or b.get("time_s") is None:
                return None
            t0, t1 = a["time_s"], b["time_s"]
            if t1 <= t0:
                return None
            t0, t1 = max(t_min, t0), min(t_max, t1)
            return (t0, t1) if t1 > t0 else None

        dev = span("FC_START", "DROP")
        if dev:
            c.create_rectangle(x_of(dev[0]), top, x_of(dev[1]), bottom, fill=_DEV_BAND, outline="")
        cool = span("DROP", "COOL_END")
        if cool:
            c.create_rectangle(x_of(cool[0]), top, x_of(cool[1]), bottom, fill=_COOL_BAND, outline="")

    @staticmethod
    def _draw_axes(c, left, right, temp_top, temp_bottom, body_bottom,
                   temp_lo, temp_hi, ror_lo, ror_hi, t_min, t_max, x_of) -> None:
        tick = sp(4)
        label_gap = sp(6)
        for i in range(5):
            frac = i / 4
            y = temp_bottom - frac * (temp_bottom - temp_top)
            v = temp_lo + frac * (temp_hi - temp_lo)
            c.create_line(left - tick, y, left, y, fill=MUTED)
            c.create_text(left - label_gap, y, text=f"{v:.0f}", font=_TICK_FONT, fill=MUTED, anchor="e")
            rv = ror_lo + frac * (ror_hi - ror_lo)
            c.create_line(right, y, right + tick, y, fill=MUTED)
            c.create_text(right + label_gap, y, text=f"{rv:.0f}", font=_TICK_FONT, fill=MUTED, anchor="w")

        c.create_line(left, temp_bottom, right, temp_bottom, fill=_GRID)

        min_tick_spacing = sp(90)
        n_ticks = max(2, int((right - left) // min_tick_spacing))
        for i in range(n_ticks + 1):
            t = t_min + (i / n_ticks) * (t_max - t_min)
            x = x_of(t)
            c.create_line(x, body_bottom, x, body_bottom + tick, fill=MUTED)
            c.create_text(x, body_bottom + label_gap, text=curves.format_mmss(t), font=_TICK_FONT,
                          fill=MUTED, anchor="n")

    @staticmethod
    def _step_coords(points: list[tuple[float, float]], x_of, y_of, t_end: float) -> list[float]:
        if not points:
            return []
        coords: list[float] = []
        t0, v0 = points[0]
        coords += [x_of(t0), y_of(v0)]
        for t, v in points[1:]:
            coords += [x_of(t), y_of(v0)]
            coords += [x_of(t), y_of(v)]
            v0 = v
        coords += [x_of(t_end), y_of(v0)]
        return coords

    def _draw_controls(self, c, controls, x_of, y_ctrl, t_max) -> None:
        for label in _CONTROL_ORDER:
            points = controls.get(label)
            if not points:
                continue
            coords = self._step_coords(points, x_of, y_ctrl, t_max)
            if len(coords) >= 4:
                c.create_line(*coords, fill=_CONTROL_COLORS.get(label, MUTED), width=lw(2))

    @staticmethod
    def _draw_ror(c, times, ror, x_of, y_ror) -> None:
        if not ror:
            return
        width = lw(1)
        coords: list[float] = []
        for t, v in zip(times, ror):
            if v is None:
                if len(coords) >= 4:
                    c.create_line(*coords, fill=_ROR_COLOR, width=width)
                coords = []
                continue
            coords += [x_of(t), y_ror(v)]
        if len(coords) >= 4:
            c.create_line(*coords, fill=_ROR_COLOR, width=width)

    @staticmethod
    def _draw_temp_line(c, times, values, x_of, y_temp, color, width) -> None:
        coords: list[float] = []
        for t, v in zip(times, values):
            if v is None:
                if len(coords) >= 4:
                    c.create_line(*coords, fill=color, width=width)
                coords = []
                continue
            coords += [x_of(t), y_temp(v)]
        if len(coords) >= 4:
            c.create_line(*coords, fill=color, width=width)

    _MILESTONE_LABELS = {
        "CHARGE": "CHARGE", "TP": "TP", "DRY_END": "DE", "FC_START": "FCs",
        "FC_END": "FCe", "SC_START": "SCs", "SC_END": "SCe", "DROP": "DROP",
    }

    def _draw_milestones(self, c, milestones, x_of, y_temp, t_min, t_max, temp_top) -> None:
        ordered = [
            m for m in milestones
            if m.get("name") in self._MILESTONE_LABELS
            and m.get("time_s") is not None and m.get("bt_c") is not None
            and t_min <= m["time_s"] <= t_max
        ]
        ordered.sort(key=lambda m: m["time_s"])
        dot_r = sp(2)
        row_h = sp(12)
        dash = (sp(2), sp(2))
        for i, m in enumerate(ordered):
            x, y = x_of(m["time_s"]), y_temp(m["bt_c"])
            label_y = temp_top + sp(9) + (i % 2) * row_h
            c.create_line(x, label_y + row_h, x, y, fill=MUTED, dash=dash)
            c.create_oval(x - dot_r, y - dot_r, x + dot_r, y + dot_r, fill=_BT_COLOR, outline="")
            c.create_text(x, label_y, text=f"{self._MILESTONE_LABELS[m['name']]} {m['bt_c']:.1f}°{self._unit}",
                          font=_LABEL_FONT, fill=FG, anchor="s")

    @staticmethod
    def _draw_legend(c, body_right, body_top, has_ror, has_sv, controls) -> None:
        # BT/ET/RoR/SV are universal roasting notation (identical in every
        # language, including French Artisan) -- not translated, same
        # reasoning as the milestone abbreviations in _MILESTONE_LABELS.
        items = [("BT", _BT_COLOR), ("ET", _ET_COLOR)]
        if has_ror:
            items.append(("ΔBT", _ROR_COLOR))
        if has_sv:
            items.append(("SV", _SV_COLOR))
        for label in _CONTROL_ORDER:
            if controls.get(label):
                items.append((t(label), _CONTROL_COLORS.get(label, MUTED)))

        swatch_w = sp(14)
        text_gap = sp(18)
        row_h = sp(14)  # a bit more than the font's own linespace so legend rows don't crowd each other
        swatch_width = lw(2)
        lx, ly = body_right - sp(72), body_top + sp(6)
        for label, color in items:
            c.create_line(lx, ly, lx + swatch_w, ly, fill=color, width=swatch_width)
            c.create_text(lx + text_gap, ly, text=label, font=_LABEL_FONT, fill=FG, anchor="w")
            ly += row_h

    # -- hover --------------------------------------------------------------

    def _on_motion(self, event: tk.Event) -> None:
        tr = self._transform
        s = self._series
        if tr is None or not s["times"]:
            return
        if event.x < tr["body_left"] or event.x > tr["body_right"]:
            self._on_leave()
            return

        # Named hover_t, not t -- gui/i18n.t is imported into this module's
        # namespace, and fmt_ror below (a closure over this scope) needs to
        # call it; a local `t` here would shadow it silently.
        frac = (event.x - tr["body_left"]) / (tr["body_right"] - tr["body_left"])
        hover_t = tr["t_min"] + frac * (tr["t_max"] - tr["t_min"])
        times = s["times"]
        i = bisect.bisect_left(times, hover_t)
        i = max(0, min(i, len(times) - 1))
        if i > 0 and abs(times[i - 1] - hover_t) < abs(times[i] - hover_t):
            i -= 1

        bt = s["bt_c"][i] if i < len(s["bt_c"]) else None
        et = s["et_c"][i] if i < len(s["et_c"]) else None
        ror = s["ror"][i] if i < len(s["ror"]) else None
        fire = curves.value_at(s["controls"].get("Burner") or [], times[i])
        drum = curves.value_at(s["controls"].get("Drum") or [], times[i])
        air = curves.value_at(s["controls"].get("Air") or [], times[i])

        def fmt_temp(v):
            return f"{v:.1f}°{self._unit}" if v is not None else "--"

        def fmt_pct(v):
            return f"{v:.0f}%" if v is not None else "--"

        def fmt_ror(v):
            return t("{v}°{unit}/min", v=f"{v:.1f}", unit=self._unit) if v is not None else "--"

        self.readout_line1.set(
            t("{time}   BT {bt}   ET {et}   ΔBT {ror}",
              time=curves.format_mmss(times[i]), bt=fmt_temp(bt), et=fmt_temp(et), ror=fmt_ror(ror))
        )
        # Burner, not "Fire" -- matches the legend, which names the same
        # channel (controls["Burner"]) that way. Both used to say
        # different things about the same data at the same time on screen.
        self.readout_line2.set(
            t("{label_burner} {burner}   {label_drum} {drum}   {label_air} {air}",
              label_burner=t("Burner"), burner=fmt_pct(fire),
              label_drum=t("Drum"), drum=fmt_pct(drum),
              label_air=t("Air"), air=fmt_pct(air))
        )

        c = self.plot
        c.delete("hover")
        x = tr["x_of"](times[i])
        c.create_line(x, tr["temp_top"], x, tr["temp_bottom"], fill="#999999",
                      dash=(sp(3), sp(2)), tags="hover")
        y_temp = tr["y_temp"]
        dot_r = sp(3)
        dot_w = lw(2)
        for v, color in ((bt, _BT_COLOR), (et, _ET_COLOR)):
            if v is None:
                continue
            y = y_temp(v)
            c.create_oval(x - dot_r, y - dot_r, x + dot_r, y + dot_r, outline=color, width=dot_w,
                          tags="hover")

    def _on_leave(self, _event: tk.Event | None = None) -> None:
        self.plot.delete("hover")
        self.readout_line1.set(t("Hover the chart for a reading at that time."))
        self.readout_line2.set("")
