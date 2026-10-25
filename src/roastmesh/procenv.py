"""The environment to launch a non-roastmesh program with, when frozen.

A PyInstaller-frozen roastmesh (CLI or GUI) sets LD_LIBRARY_PATH to point at
its own self-extracted temp directory, so its bundled .so files (built for
roastmesh's own Python/cryptography/etc.) are what get found first -- this
is confirmed as the cause of three real bugs: opening a roast crashed with
"openssl not found" / libcrypto.so errors because xdg-open picked up
roastmesh's bundled libcrypto over the system's own; `roastmesh background
enable` crashed systemctl the same way when run directly (e.g. from
install.sh); and the Settings tab's background checkbox silently read back
"disabled" on every launch even with the above fixed, because roastmesh-gui
launches the roastmesh CLI as a *child* process (gui/runner.py's
roastmesh_argv) -- so the CLI, itself frozen, inherits the GUI's own bundled
LD_LIBRARY_PATH as its starting value, and PyInstaller's bootloader then
saves *that* as the CLI's own LD_LIBRARY_PATH_ORIG. "Restore ORIG" then
restored the GUI's bundle dir, not a genuinely clean value -- still
roastmesh's own OpenSSL, just one directory removed. Confirmed directly:
LD_LIBRARY_PATH_ORIG held a `/tmp/_MEI...` path (PyInstaller's own
extraction-dir naming) in exactly this nested-launch case.

subprocess.Popen/run inherit the parent's environment by default, so every
external program launched this way (xdg-open, flatpak, systemctl, loginctl,
schtasks, a plain Artisan binary) picks this up, whether the frozen process
launching it is a direct invocation or itself a child of another frozen
roastmesh process.
"""
from __future__ import annotations

import os
import sys


def _is_pyinstaller_extraction_dir(path: str) -> bool:
    """True if `path` (or any of its ':'-separated entries) is itself a
    PyInstaller onefile temp extraction directory -- named `_MEI<random>`,
    always directly under the OS temp dir. Such a value is never a
    genuinely external LD_LIBRARY_PATH worth preserving, however it got
    here (set directly by this process's own bootloader, or inherited from
    a frozen parent whose bootloader set it first)."""
    return any(os.path.basename(entry).startswith("_MEI") for entry in path.split(os.pathsep) if entry)


def external_subprocess_env() -> dict[str, str] | None:
    """Returns None (meaning "use the parent's environment, unmodified")
    when not running frozen, so this has zero effect on a from-source run."""
    if not getattr(sys, "frozen", False):
        return None
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None and not _is_pyinstaller_extraction_dir(original):
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env
