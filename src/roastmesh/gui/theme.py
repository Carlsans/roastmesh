"""Light/dark design tokens + Sun Valley ttk chrome.

Colours are read at widget-creation time as module attributes (`theme.BG`,
`theme.FG`, ...), never imported by value. This is the same rule `widgets.py`
documents for `UI_SCALE`, for the same reason: `apply()` reassigns these when the
theme changes, and a `from theme import BG` would freeze the old value.
Consumers do `from roastmesh.gui import theme` and reference `theme.BG`.

The look: warm, coffee-forward neutrals sitting on Sun Valley's light/dark
surfaces, plus roast-phase colours that echo Artisan's drying / Maillard /
development language so an Artisan user reads the chart without relearning.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# Tokens per theme. Keep the two dicts key-for-key identical -- retheme() maps a
# widget's current colour to the matching token in the other palette by value.
_LIGHT: dict[str, str] = {
    "BG": "#faf9f7", "SURFACE": "#ffffff", "FG": "#241f1c", "MUTED": "#6b625c",
    "ACCENT": "#7a4a2b", "BORDER": "#e6e1db",
    "CONSOLE_BG": "#211d1a", "CONSOLE_FG": "#e6ded6",
    "ROW_ALT": "#f3efea", "SELECT": "#e7d9cc",
    "BT": "#c8102e", "ET": "#1f5fa9", "ROR": "#e0607e", "SV": "#ff9300",
    "PHASE_DRY": "#f6e7c8", "PHASE_MAILLARD": "#e9c9a3", "PHASE_DEV": "#d8a679",
    "PHASE_COOL": "#dce9f5", "GRID": "#e2ddd6",
    # Attention banner (e.g. an available update). Deliberately identical in
    # both palettes -- a red bar reads as "act on this" on either ground, and
    # retheme() then maps it to itself so it stays red across a theme switch.
    "DANGER": "#c62828", "DANGER_FG": "#ffece9",
}
_DARK: dict[str, str] = {
    "BG": "#1a1613", "SURFACE": "#221d19", "FG": "#ece4dc", "MUTED": "#a99e95",
    "ACCENT": "#d99a6c", "BORDER": "#352d27",
    "CONSOLE_BG": "#12100e", "CONSOLE_FG": "#d8cfc6",
    "ROW_ALT": "#241f1b", "SELECT": "#3a2e24",
    "BT": "#ff5a6e", "ET": "#5b9be0", "ROR": "#f08aa6", "SV": "#ffb04d",
    "PHASE_DRY": "#3a3222", "PHASE_MAILLARD": "#463522", "PHASE_DEV": "#523a22",
    "PHASE_COOL": "#22303f", "GRID": "#2c2620",
    "DANGER": "#c62828", "DANGER_FG": "#ffece9",
}

_PALETTES = {"light": _LIGHT, "dark": _DARK}

# Live token attributes -- default to light so any reference before apply() runs
# resolves. apply() overwrites the whole set.
BG = _LIGHT["BG"]
SURFACE = _LIGHT["SURFACE"]
FG = _LIGHT["FG"]
MUTED = _LIGHT["MUTED"]
ACCENT = _LIGHT["ACCENT"]
BORDER = _LIGHT["BORDER"]
CONSOLE_BG = _LIGHT["CONSOLE_BG"]
CONSOLE_FG = _LIGHT["CONSOLE_FG"]
ROW_ALT = _LIGHT["ROW_ALT"]
SELECT = _LIGHT["SELECT"]
BT = _LIGHT["BT"]
ET = _LIGHT["ET"]
ROR = _LIGHT["ROR"]
SV = _LIGHT["SV"]
PHASE_DRY = _LIGHT["PHASE_DRY"]
PHASE_MAILLARD = _LIGHT["PHASE_MAILLARD"]
PHASE_DEV = _LIGHT["PHASE_DEV"]
PHASE_COOL = _LIGHT["PHASE_COOL"]
GRID = _LIGHT["GRID"]
DANGER = _LIGHT["DANGER"]
DANGER_FG = _LIGHT["DANGER_FG"]

_current = "light"

# Theme settings the user can pick. "system" resolves to light/dark at apply().
SETTINGS = ("system", "light", "dark")


def current() -> str:
    """The resolved theme actually in effect ("light" or "dark")."""
    return _current


def resolve(setting: str, root: tk.Misc | None = None) -> str:
    """Map a `theme` setting ("system"/"light"/"dark") to a concrete mode.

    "system" follows the OS where we can tell, else light. Tk exposes the macOS
    and Windows appearance via `tk::unsupported::MacWindowStyle`/registry only
    awkwardly; we use the cheap, reliable signals and fall back to light -- a
    wrong guess is one toggle away, never a crash.
    """
    if setting == "dark":
        return "dark"
    if setting == "light":
        return "light"
    # system
    try:
        if root is not None and root.tk.call("tk", "windowingsystem") == "aqua":
            # macOS: this returns 'Dark' when the OS is in dark mode.
            appearance = root.tk.call("tk::unsupported::MacWindowStyle", "isdark", root)
            return "dark" if appearance else "light"
    except Exception:  # noqa: BLE001 -- any probing failure just means "assume light"
        pass
    return "light"


def _set_tokens(mode: str) -> None:
    palette = _PALETTES.get(mode, _LIGHT)
    globals().update(palette)
    globals()["_current"] = mode


def apply(root: tk.Misc, setting: str) -> str:
    """Apply the theme for `setting` to `root`: set Sun Valley's chrome, load our
    token palette, and configure the custom ttk styles the app relies on.
    Returns the resolved mode ("light"/"dark"). Call before building the UI, and
    again (with retheme) on a runtime switch."""
    mode = resolve(setting, root)
    _set_tokens(mode)
    try:
        import sv_ttk
        sv_ttk.set_theme(mode)
    except Exception:  # noqa: BLE001 -- theme is cosmetic; never fail startup over it
        pass
    _configure_styles(root)
    return mode


def _configure_styles(root: tk.Misc) -> None:
    """Custom ttk.Style tweaks layered on top of Sun Valley: readable Treeview
    rows sized to the current font, token-coloured selection, and a subtle
    heading. Kept here (not scattered in app.py) so a theme switch re-runs them
    in one place."""
    style = ttk.Style(root)
    try:
        row_font = tkfont.nametofont("TkDefaultFont")
        style.configure("Treeview", rowheight=round(row_font.metrics("linespace") * 1.35))
    except Exception:  # noqa: BLE001
        pass
    # Selection colour on tables, tokenised so it tracks the theme.
    style.map("Treeview",
              background=[("selected", SELECT)],
              foreground=[("selected", FG)])


# Colour options a tk widget might carry that should track the theme.
_COLOR_OPTIONS = (
    "background", "foreground", "activebackground", "activeforeground",
    "disabledforeground", "highlightbackground", "highlightcolor",
    "insertbackground", "selectbackground", "selectforeground", "readonlybackground",
)


def retheme(root: tk.Misc, previous: str, mode: str) -> None:
    """Live-switch an already-built widget tree from `previous` to `mode`.

    Works without knowing any widget's role: a widget's *current* colour value
    identifies its token, so we map every colour in the old palette to the same
    token in the new one and walk the tree reassigning matches. ttk widgets are
    handled by Sun Valley itself (their cget raises here and is skipped); raw tk
    widgets are recoloured in place. Canvas contents (the chart) redraw
    themselves -- callers trigger that separately.
    """
    old, new = _PALETTES.get(previous, _LIGHT), _PALETTES.get(mode, _LIGHT)
    remap = {old[k]: new[k] for k in old}

    def _walk(w: tk.Misc) -> None:
        for opt in _COLOR_OPTIONS:
            try:
                cur = str(w.cget(opt))
            except Exception:  # noqa: BLE001 -- widget doesn't have this option (e.g. ttk)
                continue
            if cur in remap and remap[cur] != cur:
                try:
                    w.configure({opt: remap[cur]})
                except Exception:  # noqa: BLE001
                    pass
        for child in w.winfo_children():
            _walk(child)

    _walk(root)
