"""First-run setup wizard.

Shown once, the first time roastmesh starts (no gui_config.json yet). A small,
skippable, step-by-step dialog that lets a new user choose the few settings that
matter -- where their Artisan roasts live, whether to reach the internet, and the
basics -- each explained in plain language. Skipping (or closing) keeps every
default and never shows it again.

Deliberately self-contained and modal: it runs *before* the main window's tabs
are built (RoastmeshApp.__init__), so it returns a finished GuiConfig the app then
saves and builds from.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog
import tkinter as tk
from tkinter import ttk

from roastmesh.gui import i18n, theme
from roastmesh.gui.config import GuiConfig
from roastmesh.gui.i18n import t
from roastmesh.gui.widgets import FONT, FONT_H1, FONT_H2, FONT_SMALL, screen_geometry, sp


def _guess_artisan_folder() -> str | None:
    """Best-effort: where Artisan users often keep their .alog files. Artisan has
    no single fixed folder, so this only offers a starting point the user can
    change -- the first candidate that actually exists, or None."""
    home = Path.home()
    candidates = [
        home / "Documents" / "Artisan",
        home / "Documents" / "artisan",
        home / "Artisan",
        home / "Documents",
    ]
    for c in candidates:
        try:
            if c.is_dir():
                return str(c)
        except OSError:
            continue
    return None


class _Wizard(tk.Toplevel):
    def __init__(self, master: tk.Misc, cfg: GuiConfig) -> None:
        super().__init__(master)
        self.title(t("Welcome to roastmesh"))
        self.configure(bg=theme.BG)
        self.transient(master)
        self.finished = False
        self._cfg = cfg

        # Collected settings, seeded from the defaults.
        self.watch_dir = tk.StringVar(value=_guess_artisan_folder() or cfg.watch_dir)
        self.wan = tk.BooleanVar(value=cfg.wan_discovery_enabled)
        self.theme_var = tk.StringVar(value=cfg.theme)
        self.language = tk.StringVar(value=i18n.current_language())
        self.temp_unit = tk.StringVar(value=cfg.temp_unit)
        self.scale = tk.StringVar(value="")   # blank = keep auto-detect

        self._step = 0
        self._steps = [self._step_welcome, self._step_folder, self._step_wan, self._step_basics]

        self._body = ttk.Frame(self)
        self._body.pack(fill="both", expand=True, padx=sp(18), pady=sp(14))

        nav = ttk.Frame(self)
        nav.pack(fill="x", padx=sp(18), pady=(0, sp(14)))
        self._skip_btn = ttk.Button(nav, text=t("Skip (use defaults)"), command=self._on_skip)
        self._skip_btn.pack(side="left")
        self._next_btn = ttk.Button(nav, text=t("Next"), command=self._on_next)
        self._next_btn.pack(side="right")
        self._back_btn = ttk.Button(nav, text=t("Back"), command=self._on_back)
        self._back_btn.pack(side="right", padx=(0, sp(8)))

        self.protocol("WM_DELETE_WINDOW", self._on_skip)
        self._render()
        # Size after the content exists so the geometry actually sticks (a
        # transient set before layout can collapse to 1x1 on some window
        # managers), then go modal.
        self.update_idletasks()
        self.geometry(screen_geometry(self, 640, 560))
        self.grab_set()
        self.focus_set()

    # -- navigation ---------------------------------------------------------

    def _render(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        self._steps[self._step]()
        self._back_btn.state(["!disabled"] if self._step > 0 else ["disabled"])
        self._next_btn.configure(text=t("Finish") if self._step == len(self._steps) - 1 else t("Next"))

    def _on_next(self) -> None:
        if self._step < len(self._steps) - 1:
            self._step += 1
            self._render()
        else:
            self.finished = True
            self.destroy()

    def _on_back(self) -> None:
        if self._step > 0:
            self._step -= 1
            self._render()

    def _on_skip(self) -> None:
        self.finished = False
        self.destroy()

    # -- steps --------------------------------------------------------------

    def _title(self, text: str, sub: str = "") -> None:
        tk.Label(self._body, text=text, font=FONT_H1, fg=theme.ACCENT, bg=theme.BG,
                 anchor="w").pack(fill="x", pady=(0, sp(4)))
        if sub:
            tk.Label(self._body, text=sub, font=FONT, fg=theme.MUTED, bg=theme.BG, anchor="w",
                     wraplength=sp(560), justify="left").pack(fill="x", pady=(0, sp(10)))

    def _para(self, text: str) -> None:
        tk.Label(self._body, text=text, font=FONT, fg=theme.FG, bg=theme.BG, anchor="w",
                 wraplength=sp(560), justify="left").pack(fill="x", pady=(0, sp(8)))

    def _step_welcome(self) -> None:
        self._title(t("Welcome to roastmesh"),
                    t("Share and discover Artisan roast profiles, peer-to-peer."))
        self._para(t("roastmesh publishes the roasts you already log in Artisan and lets you "
                     "search everyone else's -- no account, no server, no fee."))
        self._para(t("A few quick choices set it up. You can skip and change anything later in "
                     "Settings."))

    def _step_folder(self) -> None:
        self._title(t("Your roast folder"),
                    t("Where roastmesh watches for roasts to share."))
        self._para(t("Point this at the folder where Artisan saves your .alog files, and every "
                     "roast you log there is shared automatically. If you're not sure, keep the "
                     "default -- you can drop files into it yourself."))
        row = ttk.Frame(self._body)
        row.pack(fill="x", pady=(sp(4), 0))
        ttk.Entry(row, textvariable=self.watch_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text=t("Browse..."), command=self._browse_folder).pack(side="left", padx=(sp(8), 0))

    def _browse_folder(self) -> None:
        path = filedialog.askdirectory(title=t("Choose your roast folder"), parent=self)
        if path:
            self.watch_dir.set(path)

    def _step_wan(self) -> None:
        self._title(t("Find other roasters"),
                    t("How far roastmesh looks for peers."))
        self._para(t("Leave this on. It finds and shares with roasters across the internet using "
                     "the public BitTorrent network -- no server of roastmesh's own. Off, roastmesh "
                     "only ever sees peers on your local network, which for most people is nobody."))
        ttk.Checkbutton(self._body, text=t("Find peers over the whole internet (recommended)"),
                        variable=self.wan).pack(anchor="w", pady=(sp(6), 0))

    def _step_basics(self) -> None:
        self._title(t("Appearance"), t("Make it yours -- all changeable later."))
        theme_row = ttk.Frame(self._body)
        theme_row.pack(fill="x", pady=(sp(2), sp(8)))
        tk.Label(theme_row, text=t("Theme"), font=FONT_H2, bg=theme.BG, fg=theme.FG).pack(side="left")
        for value, label in (("system", t("System")), ("light", t("Light")), ("dark", t("Dark"))):
            ttk.Radiobutton(theme_row, text=label, value=value, variable=self.theme_var).pack(
                side="left", padx=(sp(10), 0))

        lang_row = ttk.Frame(self._body)
        lang_row.pack(fill="x", pady=(sp(2), sp(8)))
        tk.Label(lang_row, text=t("Language"), font=FONT_H2, bg=theme.BG, fg=theme.FG).pack(side="left")
        for code, (native_name, _plural) in i18n.LANGUAGES.items():
            ttk.Radiobutton(lang_row, text=native_name, value=code, variable=self.language).pack(
                side="left", padx=(sp(10), 0))

        unit_row = ttk.Frame(self._body)
        unit_row.pack(fill="x", pady=(sp(2), sp(8)))
        tk.Label(unit_row, text=t("Temperature"), font=FONT_H2, bg=theme.BG, fg=theme.FG).pack(side="left")
        ttk.Radiobutton(unit_row, text=t("Celsius (°C)"), value="C", variable=self.temp_unit).pack(
            side="left", padx=(sp(10), 0))
        ttk.Radiobutton(unit_row, text=t("Fahrenheit (°F)"), value="F", variable=self.temp_unit).pack(
            side="left", padx=(sp(10), 0))
        tk.Label(self._body, text=t("A language change takes effect the next time you open roastmesh."),
                 font=FONT_SMALL, fg=theme.MUTED, bg=theme.BG, anchor="w",
                 wraplength=sp(560), justify="left").pack(fill="x", pady=(sp(6), 0))

    # -- result -------------------------------------------------------------

    def result(self) -> GuiConfig:
        """The config to save. On Finish, the user's choices; on Skip, the
        defaults unchanged."""
        if not self.finished:
            return self._cfg
        return replace(
            self._cfg,
            watch_dir=self.watch_dir.get() or self._cfg.watch_dir,
            wan_discovery_enabled=self.wan.get(),
            theme=self.theme_var.get(),
            language=self.language.get(),
            temp_unit=self.temp_unit.get(),
        )


def run(master: tk.Misc, cfg: GuiConfig) -> tuple[GuiConfig, str]:
    """Run the first-run wizard modally. Returns (config_to_save, chosen_language)
    -- the language is returned separately because it must be set on i18n before
    the main window builds, and it isn't stored on the StringVar-free path."""
    wiz = _Wizard(master, cfg)
    master.wait_window(wiz)
    result = wiz.result()
    return result, result.language
