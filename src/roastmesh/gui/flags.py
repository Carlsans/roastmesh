"""Country flag images for the peers list.

Tiny bundled PNGs (gui/data/flags/<cc>.png, famfamfam public-domain 16x11) shown
via Tk's PhotoImage -- reliable on Linux and Windows alike, unlike colour flag
emoji which classic Tk on Linux can't render. Each image is loaded once and
cached (and held by the cache, so Tk's image GC can't reclaim it), and zoomed to
match the current UI scale so it isn't a speck on a HiDPI display.
"""
from __future__ import annotations

import base64
import tkinter as tk
from importlib import resources

from roastmesh.gui import widgets

_cache: dict[str, tk.PhotoImage | None] = {}


def flag_image(cc: str | None) -> tk.PhotoImage | None:
    """A PhotoImage for an ISO-3166 alpha-2 code, or None if we have no flag for
    it. Requires a Tk root to already exist (always true when the GUI calls it)."""
    if not cc:
        return None
    key = cc.lower()
    if key in _cache:
        return _cache[key]
    img: tk.PhotoImage | None = None
    try:
        res = resources.files("roastmesh.gui.data.flags").joinpath(f"{key}.png")
        if res.is_file():
            data = base64.b64encode(res.read_bytes()).decode("ascii")
            img = tk.PhotoImage(data=data)
            zoom = max(1, round(widgets.UI_SCALE))
            if zoom > 1:
                img = img.zoom(zoom)
    except Exception:  # noqa: BLE001 -- a missing/odd flag must never break the table
        img = None
    _cache[key] = img
    return img
