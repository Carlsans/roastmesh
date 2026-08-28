# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec producing two standalone binaries:

  dist/roastnet      -- CLI. console=True so `roastnet search` etc. print to
                         a terminal. This is the artifact for a headless
                         always-on node (VPS, Pi) -- ARCHITECTURE.md's Peer
                         Discovery section.
  dist/roastnet-gui   -- GUI. console=False (windowed). This is the artifact
                         a roaster downloads and double-clicks --
                         ARCHITECTURE.md's Distribution section.

Both are onefile builds (single native binary, matches the doc most
directly). Known tradeoff: onefile self-extracts to a temp directory at
startup, which breaks on a system with a noexec-mounted /tmp -- a real but
uncommon constraint. If that ever matters, switch EXE(..., exclude_binaries=
True, ...) + COLLECT(...) (onedir mode) for the affected target instead;
not done here since it's not needed for normal desktop/VPS use.

Run with: pyinstaller packaging/roastnet.spec --clean
(from the repo root, with the `build` extra installed: pip install -e ".[build]")

This same command is what a macOS or Windows build looks like too --
PyInstaller does not cross-compile, so it has to actually run on each target
OS; this spec is written to be platform-portable but Mac/Windows outputs are
unverified from this (Linux) environment.
"""
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

# `iroh`'s Python bindings are uniffi-generated: the compiled Rust extension
# is loaded by generated glue code rather than a plain top-level `import`,
# which PyInstaller's static analysis can miss. collect_all pulls in every
# submodule, data file, and binary the package ships, which is the safe
# (if slightly larger-than-minimal) way to guarantee nothing is silently
# dropped.
from PyInstaller.utils.hooks import collect_all

iroh_datas, iroh_binaries, iroh_hiddenimports = collect_all("iroh")

# schema.sql and the gui/locales/*.json translation catalogs are both read
# at runtime via importlib.resources (index/db.py's migrate(), gui/i18n.py's
# _load_catalog()) rather than imported, so -- same story as iroh above --
# PyInstaller's static analysis has no way to know either is needed. Both
# are listed in pyproject.toml's [tool.setuptools.package-data], but that's
# a setuptools/wheel concept PyInstaller doesn't read. Must be added
# explicitly, or translations work from source and silently fall back to
# English in the packaged binary -- the artifact most users actually run.
package_datas = [
    (str(SRC / "roastnet" / "index" / "schema.sql"), "roastnet/index"),
    (str(SRC / "roastnet" / "gui" / "locales"), "roastnet/gui/locales"),
    *iroh_datas,
]

# Windows shows this in the taskbar, Alt-Tab, the title bar, Explorer and
# Add/Remove Programs; without it the app is a generic Tk feather everywhere.
# PyInstaller ignores `icon` on Linux, so this is harmless there. The .ico is
# multi-resolution (16..256) because Windows picks a different size per
# context and will scale one badly if it has to.
ICON = str(ROOT / "packaging" / "roastnet.ico")

common_kwargs = dict(
    pathex=[str(SRC)],
    binaries=iroh_binaries,
    datas=package_datas,
    hiddenimports=iroh_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    noarchive=False,
)

# -- CLI ----------------------------------------------------------------
cli_analysis = Analysis([str(ROOT / "packaging" / "entry_cli.py")], **common_kwargs)
cli_pyz = PYZ(cli_analysis.pure)
cli_exe = EXE(
    cli_pyz, cli_analysis.scripts, cli_analysis.binaries, cli_analysis.zipfiles, cli_analysis.datas,
    [], name="roastnet", console=True, icon=ICON,
)

# -- GUI ------------------------------------------------------------------
gui_analysis = Analysis(
    [str(ROOT / "packaging" / "entry_gui.py")],
    **{**common_kwargs, "hiddenimports": [*iroh_hiddenimports, "tkinter"]},
)
gui_pyz = PYZ(gui_analysis.pure)
gui_exe = EXE(
    gui_pyz, gui_analysis.scripts, gui_analysis.binaries, gui_analysis.zipfiles, gui_analysis.datas,
    [], name="roastnet-gui", console=False, icon=ICON,
)
