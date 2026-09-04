"""Shared UI pieces for the roastmesh GUI.

Ported from roastlab's gui/widgets.py (same author's sibling project) --
these are generic (a labelled input, a scrolling console, a run/cancel bar)
and know nothing about roasting or roastmesh specifically, so they carry over
unchanged. The one addition is `ResultsTable`, for search results -- nothing
in roastlab's GUI produces tabular output, so it had no equivalent.
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from roastmesh.gui import theme
from roastmesh.gui import units
from roastmesh.gui.i18n import t, tn

# A single, even type scale so sizes read as a deliberate ladder rather than a
# handful of near-identical ad-hoc values. Two steps between each: caption 9 <
# body 10 < section 12 < title 14. Everything below uses these -- no bare
# ("TkDefaultFont", 9) sprinkled around, which is what made the sizes feel
# unbalanced before.
FONT_SMALL = ("TkDefaultFont", 9)          # captions, tooltips, status lines
FONT = ("TkDefaultFont", 10)               # body text, labels, inputs
FONT_BOLD = ("TkDefaultFont", 10, "bold")  # field labels
FONT_H2 = ("TkDefaultFont", 12, "bold")    # section headings
FONT_H1 = ("TkDefaultFont", 14, "bold")    # tab title
FONT_MONO = ("TkFixedFont", 9)


# Every font in this app is specified in points, so raising Tk's global
# scaling factor (RoastmeshApp does this once, at startup) already makes
# every point-sized font -- widget labels AND Canvas text alike -- bigger
# together; confirmed empirically, not assumed: a 10pt TkDefaultFont's
# rendered line height went from 19px to 55px after a 3x scaling bump on a
# real 4K display here, where X reported a bogus ~96 DPI (its physical
# screen size was wrong, a common Linux/X quirk this project hit directly --
# so auto-computing a "correct" scale from reported DPI isn't reliable
# enough to trust; detect_ui_scale below buckets by actual screen
# *resolution* instead, which is what was wrong on a real 4K display, and
# also what was wrong the other way on a real 1080p laptop -- the same
# fixed 3x this app used to always apply regardless of screen, confirmed
# far too large there). What the scaling call does NOT touch is anything
# specified in raw pixels -- window geometry, wraplength, Treeview column
# widths, and (in gui/chart.py) hand-drawn Canvas margins/line
# widths/tick sizes. UI_SCALE is applied to all of those explicitly via
# `sp()` below so the whole app stays proportional once fonts grow.
# LINE_SCALE is derived from UI_SCALE, not independent -- by convention
# it grows slower (confirmed against a real 4K display: 3x-thicker
# strokes at 3x-bigger text looked disproportionately heavy), and this
# formula reproduces that one already-tuned data point (3.0 -> 2.0)
# exactly while extending smoothly to any other scale.
#
# Both start as inert placeholders -- the real value is resolved in
# RoastmeshApp.__init__ via resolve_ui_scale()/set_scale(), once a Tk root
# exists to ask winfo_screenwidth() (nothing here can query the screen at
# import time, before any window exists). Anything that reads UI_SCALE
# must do so at call time (sp()/lw() already do, correctly), never import
# the bare name -- see gui/chart.py's fix for what goes wrong otherwise.
UI_SCALE = 1.0
LINE_SCALE = 1.0

MIN_UI_SCALE = 0.5
MAX_UI_SCALE = 4.0
SCALE_STEP = 0.15  # one Ctrl+scroll notch or Ctrl+plus/minus press, see app.py


def detect_ui_scale(widget: tk.Widget) -> float:
    """Bucket by actual screen pixel width. Only three buckets, deliberately
    coarse -- this is a starting point a user can nudge with Ctrl+scroll or
    Ctrl+plus/minus (see app.py), not an attempt at a precise formula."""
    width = widget.winfo_screenwidth()
    if width >= 3200:
        return 3.0  # 4K and above -- the original, empirically-tuned value
    if width >= 2200:
        return 1.6  # 1440p/QHD-ish
    return 1.0  # 1080p and below -- this app's original, unscaled sizing


def resolve_ui_scale(widget: tk.Widget, configured: float | None) -> float:
    """Precedence: $ROASTMESH_UI_SCALE env var (testing/scripted-launch
    override, same convention as gui/i18n.py's $ROASTMESH_LANG) > a value
    persisted in gui_config.json (set via Ctrl+scroll/Ctrl+plus/minus,
    sticky across restarts and across screens) > detected from this
    screen's resolution."""
    env = os.environ.get("ROASTMESH_UI_SCALE", "").strip()
    if env:
        try:
            value = float(env)
        except ValueError:
            value = 0
        if value > 0:
            return value
    if configured is not None and configured > 0:
        return configured
    return detect_ui_scale(widget)


def set_scale(value: float) -> None:
    """Set UI_SCALE (clamped) and derive LINE_SCALE from it. Must run
    before any widget that calls sp()/lw() is constructed -- see UI_SCALE's
    docstring above."""
    global UI_SCALE, LINE_SCALE
    UI_SCALE = max(MIN_UI_SCALE, min(MAX_UI_SCALE, value))
    LINE_SCALE = 1 + (UI_SCALE - 1) * 0.5


def sp(px: float) -> int:
    """Scale a raw pixel measurement (window geometry, wraplength, column
    width) by UI_SCALE. Never used for font point sizes -- those scale
    globally via `tk scaling` instead, see UI_SCALE's docstring above."""
    return max(1, round(px * UI_SCALE))


def lw(px: float) -> int:
    """Scale a stroke/line width by LINE_SCALE (grows slower than
    UI_SCALE -- see UI_SCALE's docstring)."""
    return max(1, round(px * LINE_SCALE))


def screen_geometry(widget: tk.Widget, width_px: int, height_px: int) -> str:
    """A `sp()`-scaled "WxH" geometry string, capped to 90% of the actual
    screen so a large UI_SCALE (a window whose *unscaled* size was already
    most of a normal screen) can't request something taller or wider than
    the display itself -- still resizable afterward either way, this is
    only the initial size hint."""
    max_w = int(widget.winfo_screenwidth() * 0.9)
    max_h = int(widget.winfo_screenheight() * 0.9)
    return f"{min(sp(width_px), max_w)}x{min(sp(height_px), max_h)}"


def maximize(window) -> bool:
    """Open `window` maximized on Windows. Returns whether it took effect.

    Windows only, deliberately. Tk's "zoomed" state is well defined there,
    whereas on Linux it depends on the window manager -- several ignore it,
    some report success and do nothing -- and the sized-and-centred default
    is what the X11 side has been used and tuned against all along. Changing
    that for everyone to fix Windows would be trading a known-good behaviour
    for an untested one.

    Never fatal: a window that opens at its normal size is a cosmetic
    disappointment, not a reason to fail to start.
    """
    if sys.platform != "win32":
        return False
    try:
        window.state("zoomed")
        return True
    except tk.TclError:
        return False


class Tooltip:
    """A hover tooltip: complementary detail shown only when the pointer rests on
    a widget, so forms stay uncluttered (progressive disclosure). Bound to a
    small info affordance rather than a whole field, so moving onto the entry
    never makes it flicker. Themed, and torn down with its widget."""

    def __init__(self, widget: tk.Widget, text: str, *, delay_ms: int = 400) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay_ms
        self._after: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _e: tk.Event | None = None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _show(self) -> None:
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + sp(14)
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + sp(4)
            screen_w = self.widget.winfo_screenwidth()
        except tk.TclError:
            return
        tip = tk.Toplevel(self.widget)
        # Built withdrawn, then positioned, then shown -- so it never flashes at
        # a default position (or unpainted) before landing where it belongs.
        tip.withdraw()
        tip.wm_overrideredirect(True)
        # Outer colour is the 1px border; the label is the body -- so the popup
        # never shows the system default (black) at its edges.
        tip.configure(bg=theme.BORDER)
        tk.Label(tip, text=self.text, font=FONT_SMALL, justify="left",
                 bg=theme.SURFACE, fg=theme.FG, wraplength=sp(360),
                 padx=sp(9), pady=sp(6)).pack(padx=1, pady=1)
        # Keep it on-screen: an icon near the right edge would otherwise push
        # the popup off the display (measured after layout, then clamped).
        tip.update_idletasks()
        w = tip.winfo_reqwidth()
        if x + w > screen_w - sp(8):
            x = max(sp(8), screen_w - w - sp(8))
        tip.wm_geometry(f"+{x}+{y}")
        tip.deiconify()
        self._tip = tip

    def _hide(self, _e: tk.Event | None = None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None


def bind_tooltip(widget: tk.Widget, text: str) -> None:
    if text:
        Tooltip(widget, text)


def _info_icon(parent: tk.Widget) -> tk.Label:
    """The small muted affordance that reveals a field's help on hover."""
    return tk.Label(parent, text="\u24d8", font=FONT_SMALL, fg=theme.MUTED,
                    bg=theme.BG, cursor="question_arrow")


def heading(parent: tk.Widget, text: str, sub: str = "") -> ttk.Frame:
    """A tab's title and one-line purpose."""
    frame = ttk.Frame(parent)
    frame.pack(fill="x", padx=14, pady=(12, 4))
    tk.Label(frame, text=text, font=FONT_H1, fg=theme.ACCENT, bg=theme.BG, anchor="w").pack(fill="x")
    if sub:
        tk.Label(frame, text=sub, font=FONT, fg=theme.MUTED, bg=theme.BG, anchor="w",
                 wraplength=sp(900), justify="left").pack(fill="x", pady=(2, 0))
    return frame


def explain(parent: tk.Widget, text: str) -> tk.Label:
    """Plain-language explanation of a screen, shown only on hover. Replaces a
    big always-on paragraph with a small hoverable "\u24d8" affordance so the
    screen stays uncluttered but the help is one hover away."""
    lbl = tk.Label(parent, text="\u24d8", font=FONT_SMALL,
                   fg=theme.MUTED, bg=theme.BG, anchor="w", cursor="question_arrow")
    lbl.pack(anchor="w", padx=14, pady=(2, 4))
    bind_tooltip(lbl, text.strip())
    return lbl


def section(parent: tk.Widget, title: str) -> ttk.LabelFrame:
    frame = ttk.LabelFrame(parent, text=title)
    frame.pack(fill="x", padx=14, pady=6)
    return frame


class Field(ttk.Frame):
    """A labelled entry with its own help line underneath.

    The help line is not optional: an input whose meaning isn't obvious from
    its label is an input the user will get wrong.
    """

    def __init__(self, parent: tk.Widget, label: str, default: str = "",
                 help_text: str = "", width: int = 28,
                 variable: tk.StringVar | None = None) -> None:
        super().__init__(parent)
        self.pack(fill="x", padx=10, pady=(6, 2))
        row = ttk.Frame(self)
        row.pack(fill="x")
        tk.Label(row, text=label, font=FONT_BOLD, bg=theme.BG, fg=theme.FG,
                 anchor="w").pack(side="left")
        # `variable`, when given, is a StringVar owned by the caller (e.g.
        # RoastmeshApp) rather than one this Field creates for itself -- lets
        # a setting shown here (Settings tab) be the exact same variable
        # every other tab reads, so there's one source of truth instead of
        # needing to keep two in sync.
        if variable is not None:
            self.var = variable
            if default and not self.var.get():
                self.var.set(default)
        else:
            self.var = tk.StringVar(value=default)
        self.entry = ttk.Entry(row, textvariable=self.var, width=width)
        self.entry.pack(side="left", fill="x", expand=True)
        if help_text:
            icon = _info_icon(row)
            icon.pack(side="right", padx=(sp(6), 0))
            bind_tooltip(icon, help_text)

    def get(self) -> str:
        return self.var.get().strip()

    def set(self, value: str) -> None:
        self.var.set(value)


class Choice(ttk.Frame):
    """A labelled dropdown with help text, same contract as Field."""

    def __init__(self, parent: tk.Widget, label: str, options: list[str],
                 default: str = "", help_text: str = "") -> None:
        super().__init__(parent)
        self.pack(fill="x", padx=10, pady=(6, 2))
        row = ttk.Frame(self)
        row.pack(fill="x")
        tk.Label(row, text=label, font=FONT_BOLD, bg=theme.BG, fg=theme.FG,
                 anchor="w").pack(side="left")
        self.var = tk.StringVar(value=default or (options[0] if options else ""))
        ttk.Combobox(row, textvariable=self.var, values=options, width=26,
                     state="readonly").pack(side="left")
        if help_text:
            icon = _info_icon(row)
            icon.pack(side="right", padx=(sp(6), 0))
            bind_tooltip(icon, help_text)

    def get(self) -> str:
        return self.var.get().strip()


class AutocompleteField(ttk.Frame):
    """A labelled, editable dropdown that narrows its own list as you type,
    same label/help-text contract as Field.

    Choice (above) can't serve this need: it is `state="readonly"` (blocks
    typing a value that isn't already in the list -- e.g. a machine not in
    the catalogue), takes no external `variable=` (so Settings and Search
    can't share one value the way Field's `variable=` lets a setting be
    read from more than one place), has no `.set()`, and never keeps a
    reference to its own Combobox, so `values` can never be refreshed once
    a background `machines list` (or `machines list --used`) command
    returns. This class keeps `self.combo` for exactly that, and stays
    editable (never readonly) since a caller (Settings' machine field) must
    accept a value that isn't in the catalogue at all.
    """

    def __init__(self, parent: tk.Widget, label: str, values: list[str] | None = None,
                 default: str = "", help_text: str = "", width: int = 26,
                 variable: tk.StringVar | None = None) -> None:
        super().__init__(parent)
        self.pack(fill="x", padx=10, pady=(6, 2))
        row = ttk.Frame(self)
        row.pack(fill="x")
        tk.Label(row, text=label, font=FONT_BOLD, bg=theme.BG, fg=theme.FG,
                 anchor="w").pack(side="left")
        self._all_values: list[str] = list(values or [])
        if variable is not None:
            self.var = variable
            if default and not self.var.get():
                self.var.set(default)
        else:
            self.var = tk.StringVar(value=default)
        self.combo = ttk.Combobox(row, textvariable=self.var, values=self._all_values, width=width)
        self.combo.pack(side="left", fill="x", expand=True)
        # Editable (unlike Choice's readonly state), plus live filtering:
        # typing narrows the dropdown to values containing what's typed
        # anywhere in the string, not just as a prefix -- and never blocks
        # a value that isn't in the list at all (e.g. a custom machine).
        self.combo.bind("<KeyRelease>", self._on_key_release)
        if help_text:
            icon = _info_icon(row)
            icon.pack(side="right", padx=(sp(6), 0))
            bind_tooltip(icon, help_text)

    def set_values(self, values: list[str]) -> None:
        self._all_values = list(values)
        self.combo.configure(values=self._all_values)

    def _on_key_release(self, event: tk.Event) -> None:
        # Navigation/editing keys must not re-filter -- e.g. pressing Down
        # to browse the (already-filtered) dropdown shouldn't immediately
        # recompute it from whatever partial text happens to be typed.
        if event.keysym in ("Up", "Down", "Left", "Right", "Return", "Tab", "Escape"):
            return
        typed = self.var.get()
        filtered = self._filter_values(self._all_values, typed)
        self.combo.configure(values=filtered)
        # Show the filtered suggestions live as the user types -- post the
        # dropdown, then hand focus straight back to the entry so typing keeps
        # going. Only when there's text and something to suggest.
        if typed and filtered:
            try:
                self.combo.tk.call("ttk::combobox::Post", self.combo)
                self.combo.focus_set()
                self.combo.icursor("end")
            except tk.TclError:
                pass

    @staticmethod
    def _filter_values(values: list[str], typed: str) -> list[str]:
        """Pure filter logic, kept separate from _on_key_release so it can
        be unit-tested without a live display (see tests/test_widgets.py)."""
        if not typed:
            return list(values)
        typed_lower = typed.lower()
        return [v for v in values if typed_lower in v.lower()]

    def get(self) -> str:
        return self.var.get().strip()

    def set(self, value: str) -> None:
        self.var.set(value)


class Console(ttk.Frame):
    """Scrolling output area with the command that produced it shown above.

    Showing the command is a deliberate anti-black-box measure: anything the
    GUI does, the user can reproduce and script in a terminal.
    """

    def __init__(self, parent: tk.Widget, height: int = 14) -> None:
        super().__init__(parent)
        self.pack(fill="both", expand=True, padx=14, pady=(4, 12))

        self.cmd_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.cmd_var, font=("TkFixedFont", 8),
                 fg=theme.MUTED, bg=theme.BG, anchor="w", wraplength=sp(900),
                 justify="left").pack(fill="x", pady=(0, 3))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.text = tk.Text(wrap, height=height, wrap="none", bg=theme.CONSOLE_BG,
                            fg=theme.CONSOLE_FG, insertbackground=theme.CONSOLE_FG,
                            font=FONT_MONO, relief="flat")
        ybar = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        xbar = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")
        xbar.pack(fill="x")
        self.text.configure(state="disabled")

    def set_command(self, text: str) -> None:
        self.cmd_var.set(f"$ {text}" if text else "")

    def append(self, text: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", text)
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def get_text(self) -> str:
        return self.text.get("1.0", "end")


class RunBar(ttk.Frame):
    """Run / Cancel buttons plus a status line.

    Cancel is always present, never conditionally hidden: a network sync can
    hang, and a user who cannot stop one will kill the whole application
    instead.
    """

    def __init__(self, parent: tk.Widget, run_label: str,
                 on_run: Callable[[], None], on_cancel: Callable[[], None]) -> None:
        super().__init__(parent)
        self.pack(fill="x", padx=14, pady=(8, 2))
        self.run_btn = ttk.Button(self, text=run_label, command=on_run)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(self, text=t("Cancel"), command=on_cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.status = tk.StringVar(value=t("ready"))
        tk.Label(self, textvariable=self.status, font=FONT, fg=theme.MUTED, bg=theme.BG,
                 anchor="w").pack(side="left", padx=(14, 0))

    def set_running(self, running: bool, status: str = "") -> None:
        self.run_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")
        if status:
            self.status.set(status)


def scrollable(parent: tk.Widget) -> ttk.Frame:
    """A vertically scrollable region, for tabs with more content than fits
    on a laptop screen."""
    canvas = tk.Canvas(parent, bg=theme.BG, highlightthickness=0)
    bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    window = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")

    def _wheel(event: tk.Event) -> None:
        delta = 1 if getattr(event, "num", 0) == 5 else -1 if getattr(event, "num", 0) == 4 else 0
        if delta == 0:
            delta = -1 if getattr(event, "delta", 0) > 0 else 1
        canvas.yview_scroll(delta, "units")

    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        canvas.bind_all(seq, _wheel, add="+")
    return inner


# title, roast_date, machine_key, roast_type, dtr_pct, drop_bt_c, beans_text
# -- no roast_id column; it's still the Treeview's iid under the hood
# (double-click handlers read it back via identify_row/selection), just
# not shown, since it's meaningless to look at and title/roast_date/beans
# below are what actually identify a roast to a person. drop_bt_c's label
# is placeholder text -- ResultsTable._column_label rewrites it to match
# the selected temperature unit whenever set_rows() runs.
_COLUMNS = [
    ("title", "Title", 160),
    ("roast_date", "Roast date", 100),
    ("machine_key", "Machine", 110),
    ("roast_type", "Roast type", 90),
    ("dtr_pct", "DTR %", 60),
    ("drop_bt_c", "Drop °C", 65),
    ("beans_text", "Beans", 260),
]


class ResultsTable(ttk.Frame):
    """A search results table, sortable by clicking any column header --
    click again to reverse, and the current sort (if any) carries over to
    the next search's results too, so it doesn't reset every time you
    refine a query. A column showing numbers (DTR %, Drop °C) sorts
    numerically, not alphabetically -- see _sort_key."""

    def __init__(self, parent: tk.Widget, height: int = 14) -> None:
        super().__init__(parent)
        self.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self._sort_column: str | None = None
        self._sort_reverse = False
        self._unit = units.CELSIUS

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in _COLUMNS], show="headings", height=height,
        )
        for key, _label, width in _COLUMNS:
            self.tree.heading(key, command=lambda k=key: self._on_heading_click(k))
            self.tree.column(key, width=sp(width), anchor="w")
        self._refresh_headers()
        ybar = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        # A horizontal scrollbar too: the columns above add up to wider
        # than the window at its default size, and unlike a plain label,
        # a Treeview doesn't wrap or shrink its columns to fit on its own.
        xbar = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")
        xbar.pack(fill="x")

        self.count_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.count_var, font=FONT, fg=theme.MUTED, bg=theme.BG,
                 anchor="w").pack(fill="x", pady=(4, 0))

    def set_rows(self, rows: list[dict], unit: str = units.CELSIUS) -> None:
        self._unit = unit
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            beans = (row.get("beans_text") or "").splitlines()[0][:80] if row.get("beans_text") else ""
            title = row.get("title") or ""
            if row.get("hidden"):
                title = t("{title} (hidden)", title=title) if title else t("(hidden)")
            if row.get("blob_local") is False:
                # A stub: indexed and searchable, but the .alog bytes were
                # evicted to reclaim disk -- fetched on demand when opened.
                title = "☁ " + (title or t("(not downloaded)"))
            drop_c = units.convert_temp(row.get("drop_bt_c"), unit)
            dtr = f"{row['dtr_pct']:.1f}" if row.get("dtr_pct") is not None else ""
            drop = f"{drop_c:.0f}" if drop_c is not None else ""
            self.tree.insert("", "end", iid=row.get("roast_id"), values=(
                title, row.get("roast_date") or "", row.get("machine_key") or "",
                row.get("roast_type") or "", dtr, drop, beans,
            ))
        self.count_var.set(tn(len(rows), "{n} result", "{n} results"))
        self._refresh_headers()
        if self._sort_column is not None:
            self._apply_sort()  # keep whatever sort was active before this search ran

    def _column_label(self, key: str, label: str) -> str:
        return f"{t('Drop')} °{self._unit}" if key == "drop_bt_c" else t(label)

    def _refresh_headers(self) -> None:
        for key, label, _width in _COLUMNS:
            text = self._column_label(key, label)
            if key == self._sort_column:
                text += " ▼" if self._sort_reverse else " ▲"
            self.tree.heading(key, text=text)

    def _on_heading_click(self, column: str) -> None:
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self._apply_sort()

    @staticmethod
    def _sort_key(value: str) -> tuple[int, float | str]:
        # Numeric columns (DTR %, Drop °C) must sort as numbers, not text
        # ("10" belongs after "9", not before it) -- tag every value by
        # whether it parses as one so mixed numeric/blank columns still
        # sort sensibly (numbers grouped together, blanks trailing).
        try:
            return (0, float(value))
        except ValueError:
            return (1, value.lower())

    def _apply_sort(self) -> None:
        column = self._sort_column
        if column is None:
            return
        items = [(self.tree.set(iid, column), iid) for iid in self.tree.get_children("")]
        items.sort(key=lambda pair: self._sort_key(pair[0]), reverse=self._sort_reverse)
        for index, (_value, iid) in enumerate(items):
            self.tree.move(iid, "", index)
        self._refresh_headers()

    def set_error(self, message: str) -> None:
        self.tree.delete(*self.tree.get_children())
        self.count_var.set(message)


_PEER_COLUMNS = [
    ("ip", "IP", 150),
    ("feed_pubkey_hex", "Pubkey", 200),
    ("last_seen", "Last seen", 180),
    ("added_via", "Via", 80),
]


class PeerTable(ttk.Frame):
    """Known-peer list for the Network tab, with a country flag per peer.

    The leading tree column (#0) carries the flag image + ISO code -- Treeview
    only renders an image in #0, so that is where it goes; the rest are plain
    columns. IP/country come from the peer's ticket (peer list --json includes
    the public IP; gui.geoip maps it to a country, gui.flags to an image)."""

    def __init__(self, parent: tk.Widget, height: int = 6) -> None:
        super().__init__(parent)
        self.pack(fill="both", expand=True, padx=10, pady=(4, 8))
        # Hold every flag PhotoImage so Tk's image GC can't reclaim one that a
        # row still references (the classic "images vanish" tkinter trap).
        self._flag_refs: list = []

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in _PEER_COLUMNS], show="tree headings", height=height,
        )
        self.tree.heading("#0", text=t("Country"))
        self.tree.column("#0", width=sp(110), stretch=False, anchor="w")
        for key, label, width in _PEER_COLUMNS:
            self.tree.heading(key, text=t(label))
            self.tree.column(key, width=sp(width), anchor="w")
        ybar = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")
        xbar.pack(fill="x")

    def set_rows(self, rows: list[dict]) -> None:
        # Lazy import avoids a module cycle (flags imports widgets) and keeps the
        # geo table off the import path until a peer list is actually shown.
        from roastmesh.gui import flags, geoip
        self.tree.delete(*self.tree.get_children())
        self._flag_refs = []
        for row in rows:
            ip = row.get("ip")
            cc = geoip.country_code(ip)
            img = flags.flag_image(cc) if cc else None
            if img is not None:
                self._flag_refs.append(img)
            pubkey = row.get("feed_pubkey_hex") or "?"
            self.tree.insert(
                "", "end",
                text=(f" {cc}" if cc else ""), image=(img or ""),
                values=(ip or t("LAN / relay"),
                        (pubkey[:16] + "...") if len(pubkey) > 16 else pubkey,
                        row.get("last_seen") or "", row.get("added_via") or ""),
            )


_USER_COLUMNS = [
    ("display_name", "Name", 160),
    ("pubkey", "Pubkey", 90),
    ("machine", "Machine", 140),
    ("roast_count", "Roasts", 70),
    ("like_count", "Likes", 60),
    ("favorite", "Favorite", 60),
]


class UserTable(ttk.Frame):
    """The user list for SearchTab's "one user" mode: display name, an
    8-character pubkey prefix (names are cosmetic and never unique --
    ARCHITECTURE.md -- the prefix is what actually disambiguates), declared
    machine, roast and like counts, and a favorite marker.

    Rows are keyed by the user's full pubkey_hex as the Treeview iid --
    ResultsTable's approach, not PeerTable's (above): PeerTable sets no
    iid, so its rows can never be mapped back to the data that produced
    them, which is exactly what's needed here to drive Favorite/Like on
    whichever row is selected.
    """

    def __init__(self, parent: tk.Widget, height: int = 8) -> None:
        super().__init__(parent)
        self.pack(fill="both", expand=True, padx=10, pady=(4, 8))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in _USER_COLUMNS], show="headings", height=height,
            selectmode="browse",
        )
        for key, label, width in _USER_COLUMNS:
            self.tree.heading(key, text=t(label))
            self.tree.column(key, width=sp(width), anchor="w")
        ybar = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")

    def set_rows(self, rows: list[dict]) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            pubkey = row.get("pubkey_hex") or ""
            machine = row.get("machine_display") or row.get("machine_key") or ""
            self.tree.insert("", "end", iid=pubkey, values=(
                row.get("display_name") or t("?"),
                pubkey[:8],
                machine,
                row.get("roast_count", 0),
                row.get("like_count", 0),
                "★" if row.get("is_favorite") else "",
            ))


# A plain green works readably against both this app's light and dark
# theme.BG (confirmed against both palettes in theme.py) without needing a
# dedicated theme token of its own for what is, so far, the only place a
# status dot like this appears.
_ONLINE_DOT_COLOR = "#2e7d32"

_DEVICE_COLUMNS = [
    ("name", "Name", 170),
    ("pubkey", "Pubkey", 170),
    ("platform", "Platform", 90),
    ("paired_at", "Paired", 150),
]


class DeviceTable(ttk.Frame):
    """Paired-device list for the Devices tab: a best-effort online dot (the
    #0 column, PeerTable's leading-column-carries-a-marker convention, a
    plain colored dot here rather than a flag image) plus name/pubkey
    prefix/platform/paired-at. Rows are keyed by the device's full pubkey as
    the Treeview iid -- UserTable's convention, not PeerTable's -- so
    Remove can act on whichever row is selected."""

    def __init__(self, parent: tk.Widget, height: int = 6) -> None:
        super().__init__(parent)
        self.pack(fill="both", expand=True, padx=10, pady=(4, 8))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in _DEVICE_COLUMNS], show="tree headings", height=height,
            selectmode="browse",
        )
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=sp(26), stretch=False, anchor="center")
        for key, label, width in _DEVICE_COLUMNS:
            self.tree.heading(key, text=t(label))
            self.tree.column(key, width=sp(width), anchor="w")
        self.tree.tag_configure("online", foreground=_ONLINE_DOT_COLOR)
        self.tree.tag_configure("offline", foreground=theme.MUTED)
        ybar = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")

    def set_rows(self, rows: list[dict]) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            pubkey = row.get("pubkey") or ""
            online = bool(row.get("online"))
            self.tree.insert("", "end", iid=pubkey, text="●", tags=("online" if online else "offline",),
                             values=(
                                 row.get("name") or t("?"),
                                 (pubkey[:16] + "...") if len(pubkey) > 16 else pubkey,
                                 row.get("platform") or "",
                                 (row.get("paired_at") or "")[:10],
                             ))
