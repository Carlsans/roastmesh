# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec producing two standalone binaries:

  dist/roastmesh      -- CLI. console=True so `roastmesh search` etc. print to
                         a terminal. This is the artifact for a headless
                         always-on node (VPS, Pi) -- ARCHITECTURE.md's Peer
                         Discovery section.
  dist/roastmesh-gui   -- GUI. console=False (windowed). This is the artifact
                         a roaster downloads and double-clicks --
                         ARCHITECTURE.md's Distribution section.

Linux builds are onefile (single native binary, matches the doc most
directly). Known tradeoff: onefile self-extracts to a temp directory at
startup, which breaks on a system with a noexec-mounted /tmp -- a real but
uncommon constraint.

**Windows builds are onedir**, producing dist/roastmesh/ with both .exe files
and a shared _internal/ beside them. Not a stylistic choice: Windows Defender
quarantined the onefile installer as malware. That is a false positive, and a
well-understood one -- a onefile binary unpacks itself to %TEMP% and executes
from there, which is what a dropper does, and the stock PyInstaller bootloader
it is built from appears inside real malware, so its bytes match signatures.
Unsigned code with no download reputation gets no benefit of the doubt.
onedir removes the self-extraction behaviour entirely, which is the single
biggest lever available without paying for a code-signing certificate.

Linux deliberately stays onefile: install.sh and every release since v0.1
fetch bare `roastmesh` / `roastmesh-gui` assets by name, and Linux has no
equivalent false-positive problem to solve.

Run with: pyinstaller packaging/roastmesh.spec --clean
(from the repo root, with the `build` extra installed: pip install -e ".[build]")

This same command is what a macOS or Windows build looks like too --
PyInstaller does not cross-compile, so it has to actually run on each target
OS; this spec is written to be platform-portable but Mac/Windows outputs are
unverified from this (Linux) environment.
"""
import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).parent
ONEDIR = sys.platform == "win32"   # see the module docstring for why
SRC = ROOT / "src"

# `iroh`'s Python bindings are uniffi-generated: the compiled Rust extension
# is loaded by generated glue code rather than a plain top-level `import`,
# which PyInstaller's static analysis can miss. collect_all pulls in every
# submodule, data file, and binary the package ships, which is the safe
# (if slightly larger-than-minimal) way to guarantee nothing is silently
# dropped.
from PyInstaller.utils.hooks import collect_all

iroh_datas, iroh_binaries, iroh_hiddenimports = collect_all("iroh")
# sv_ttk (Sun Valley theme) loads Tcl theme files and PNG sprites at runtime
# by sourcing .tcl -- invisible to static analysis, same as iroh, so collect
# everything it ships or the GUI falls back to the default ttk theme in the
# packaged binary (works, but not the intended look).
sv_datas, sv_binaries, sv_hiddenimports = collect_all("sv_ttk")

# schema.sql and the gui/locales/*.json translation catalogs are both read
# at runtime via importlib.resources (index/db.py's migrate(), gui/i18n.py's
# _load_catalog()) rather than imported, so -- same story as iroh above --
# PyInstaller's static analysis has no way to know either is needed. Both
# are listed in pyproject.toml's [tool.setuptools.package-data], but that's
# a setuptools/wheel concept PyInstaller doesn't read. Must be added
# explicitly, or translations work from source and silently fall back to
# English in the packaged binary -- the artifact most users actually run.
package_datas = [
    (str(SRC / "roastmesh" / "index" / "schema.sql"), "roastmesh/index"),
    (str(SRC / "roastmesh" / "gui" / "locales"), "roastmesh/gui/locales"),
    # Offline IP->country table + flag PNGs for the peers list, loaded at
    # runtime via importlib.resources (gui/geoip.py, gui/flags.py).
    (str(SRC / "roastmesh" / "gui" / "data"), "roastmesh/gui/data"),
    *iroh_datas,
    *sv_datas,
]

# Windows shows this in the taskbar, Alt-Tab, the title bar, Explorer and
# Add/Remove Programs; without it the app is a generic Tk feather everywhere.
# PyInstaller ignores `icon` on Linux, so this is harmless there. The .ico is
# multi-resolution (16..256) because Windows picks a different size per
# context and will scale one badly if it has to.
ICON = str(ROOT / "packaging" / "roastmesh.ico")

common_kwargs = dict(
    pathex=[str(SRC)],
    binaries=[*iroh_binaries, *sv_binaries],
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

# -- GUI ------------------------------------------------------------------
gui_analysis = Analysis(
    [str(ROOT / "packaging" / "entry_gui.py")],
    **{**common_kwargs, "hiddenimports": [*iroh_hiddenimports, *sv_hiddenimports, "tkinter", "sv_ttk"]},
)
gui_pyz = PYZ(gui_analysis.pure)

if ONEDIR:
    # Both executables share one COLLECT, so they land in a single directory
    # with one copy of the runtime between them rather than two ~20MB trees.
    # That co-location is also required, not incidental: gui/runner.py's
    # roastmesh_argv() resolves the CLI as a sibling of sys.executable, so
    # roastmesh.exe must sit next to roastmesh-gui.exe or every action the
    # GUI performs breaks.
    cli_exe = EXE(
        cli_pyz, cli_analysis.scripts, [], exclude_binaries=True,
        name="roastmesh", console=True, icon=ICON,
    )
    gui_exe = EXE(
        gui_pyz, gui_analysis.scripts, [], exclude_binaries=True,
        name="roastmesh-gui", console=False, icon=ICON,
    )
    coll = COLLECT(
        cli_exe, cli_analysis.binaries, cli_analysis.zipfiles, cli_analysis.datas,
        gui_exe, gui_analysis.binaries, gui_analysis.zipfiles, gui_analysis.datas,
        strip=False, upx=False, name="roastmesh",
    )
else:
    cli_exe = EXE(
        cli_pyz, cli_analysis.scripts, cli_analysis.binaries, cli_analysis.zipfiles,
        cli_analysis.datas, [], name="roastmesh", console=True, icon=ICON,
    )
    gui_exe = EXE(
        gui_pyz, gui_analysis.scripts, gui_analysis.binaries, gui_analysis.zipfiles,
        gui_analysis.datas, [], name="roastmesh-gui", console=False, icon=ICON,
    )
