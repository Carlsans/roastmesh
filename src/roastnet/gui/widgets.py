"""Shared UI pieces for the roastnet GUI.

Ported from roastlab's gui/widgets.py (same author's sibling project) --
these are generic (a labelled input, a scrolling console, a run/cancel bar)
and know nothing about roasting or roastnet specifically, so they carry over
unchanged. The one addition is `ResultsTable`, for search results -- nothing
in roastlab's GUI produces tabular output, so it had no equivalent.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

# Palette. Deliberately muted -- this is a tool for reading search results,
# not a dashboard to be impressed by.
BG = "#f7f6f3"
FG = "#2b2b2b"
MUTED = "#6b6b6b"
ACCENT = "#7a4a2b"  # roasted-bean brown, used sparingly for headings
CONSOLE_BG = "#1e1e1e"
CONSOLE_FG = "#d8d8d8"

FONT = ("TkDefaultFont", 10)
FONT_BOLD = ("TkDefaultFont", 10, "bold")
FONT_H1 = ("TkDefaultFont", 15, "bold")
FONT_H2 = ("TkDefaultFont", 11, "bold")
FONT_MONO = ("TkFixedFont", 9)


def heading(parent: tk.Widget, text: str, sub: str = "") -> ttk.Frame:
    """A tab's title and one-line purpose."""
    frame = ttk.Frame(parent)
    frame.pack(fill="x", padx=14, pady=(12, 4))
    tk.Label(frame, text=text, font=FONT_H1, fg=ACCENT, bg=BG, anchor="w").pack(fill="x")
    if sub:
        tk.Label(frame, text=sub, font=FONT, fg=MUTED, bg=BG, anchor="w",
                 wraplength=900, justify="left").pack(fill="x", pady=(2, 0))
    return frame


def explain(parent: tk.Widget, text: str) -> tk.Label:
    """A block of plain-language explanation of what a screen is for."""
    lbl = tk.Label(parent, text=text.strip(), font=FONT, fg=FG, bg=BG,
                   wraplength=900, justify="left", anchor="w")
    lbl.pack(fill="x", padx=14, pady=(6, 4))
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
                 help_text: str = "", width: int = 28) -> None:
        super().__init__(parent)
        self.pack(fill="x", padx=10, pady=(6, 2))
        row = ttk.Frame(self)
        row.pack(fill="x")
        tk.Label(row, text=label, font=FONT_BOLD, bg=BG, fg=FG, width=22,
                 anchor="w").pack(side="left")
        self.var = tk.StringVar(value=default)
        self.entry = ttk.Entry(row, textvariable=self.var, width=width)
        self.entry.pack(side="left", fill="x", expand=True)
        if help_text:
            tk.Label(self, text=help_text, font=("TkDefaultFont", 9), fg=MUTED,
                     bg=BG, wraplength=840, justify="left", anchor="w").pack(
                fill="x", padx=(224, 0), pady=(1, 0))

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
        tk.Label(row, text=label, font=FONT_BOLD, bg=BG, fg=FG, width=22,
                 anchor="w").pack(side="left")
        self.var = tk.StringVar(value=default or (options[0] if options else ""))
        ttk.Combobox(row, textvariable=self.var, values=options, width=26,
                     state="readonly").pack(side="left")
        if help_text:
            tk.Label(self, text=help_text, font=("TkDefaultFont", 9), fg=MUTED,
                     bg=BG, wraplength=840, justify="left", anchor="w").pack(
                fill="x", padx=(224, 0), pady=(1, 0))

    def get(self) -> str:
        return self.var.get().strip()


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
                 fg=MUTED, bg=BG, anchor="w", wraplength=900,
                 justify="left").pack(fill="x", pady=(0, 3))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.text = tk.Text(wrap, height=height, wrap="none", bg=CONSOLE_BG,
                            fg=CONSOLE_FG, insertbackground=CONSOLE_FG,
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
        self.cancel_btn = ttk.Button(self, text="Cancel", command=on_cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.status = tk.StringVar(value="ready")
        tk.Label(self, textvariable=self.status, font=FONT, fg=MUTED, bg=BG,
                 anchor="w").pack(side="left", padx=(14, 0))

    def set_running(self, running: bool, status: str = "") -> None:
        self.run_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")
        if status:
            self.status.set(status)


def scrollable(parent: tk.Widget) -> ttk.Frame:
    """A vertically scrollable region, for tabs with more content than fits
    on a laptop screen."""
    canvas = tk.Canvas(parent, bg=BG, highlightthickness=0)
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


# roast_id, machine_key, roast_type, dtr_pct, drop_bt_c, beans_text
_COLUMNS = [
    ("roast_id", "ID", 90),
    ("machine_key", "Machine", 120),
    ("roast_type", "Roast type", 100),
    ("dtr_pct", "DTR %", 70),
    ("drop_bt_c", "Drop °C", 70),
    ("beans_text", "Beans", 320),
]


class ResultsTable(ttk.Frame):
    """A sortable-by-eye table of search results (roastnet has no
    equivalent in roastlab's GUI, since none of that project's commands
    produce a list of rows the way `roastnet search` does)."""

    def __init__(self, parent: tk.Widget, height: int = 14) -> None:
        super().__init__(parent)
        self.pack(fill="both", expand=True, padx=14, pady=(4, 12))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in _COLUMNS], show="headings", height=height,
        )
        for key, label, width in _COLUMNS:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")
        ybar = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")

        self.count_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.count_var, font=FONT, fg=MUTED, bg=BG,
                 anchor="w").pack(fill="x", pady=(4, 0))

    def set_rows(self, rows: list[dict]) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            beans = (row.get("beans_text") or "").splitlines()[0][:80] if row.get("beans_text") else ""
            dtr = f"{row['dtr_pct']:.1f}" if row.get("dtr_pct") is not None else ""
            drop = f"{row['drop_bt_c']:.0f}" if row.get("drop_bt_c") is not None else ""
            self.tree.insert("", "end", values=(
                (row.get("roast_id") or "")[:8], row.get("machine_key") or "",
                row.get("roast_type") or "", dtr, drop, beans,
            ))
        self.count_var.set(f"{len(rows)} result{'s' if len(rows) != 1 else ''}")

    def set_error(self, message: str) -> None:
        self.tree.delete(*self.tree.get_children())
        self.count_var.set(message)


_PEER_COLUMNS = [
    ("feed_pubkey_hex", "Pubkey", 280),
    ("last_seen", "Last seen", 220),
    ("added_via", "Via", 90),
]


class PeerTable(ttk.Frame):
    """Known-peer list for the Network tab."""

    def __init__(self, parent: tk.Widget, height: int = 6) -> None:
        super().__init__(parent)
        self.pack(fill="both", expand=True, padx=10, pady=(4, 8))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in _PEER_COLUMNS], show="headings", height=height,
        )
        for key, label, width in _PEER_COLUMNS:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")
        ybar = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")

    def set_rows(self, rows: list[dict]) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            self.tree.insert("", "end", values=(
                row.get("feed_pubkey_hex") or "?", row.get("last_seen") or "", row.get("added_via") or "",
            ))
