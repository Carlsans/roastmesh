"""The environment to launch a non-roastmesh program with, when frozen.

A PyInstaller-frozen roastmesh (CLI or GUI) sets LD_LIBRARY_PATH to point at
its own self-extracted temp directory, so its bundled .so files (built for
roastmesh's own Python/cryptography/etc.) are what get found first -- this
is confirmed as the cause of two real bugs: opening a roast crashed with
"openssl not found" / libcrypto.so errors because xdg-open picked up
roastmesh's bundled libcrypto over the system's own, and `roastmesh
background enable` crashed systemctl the same way ("version `OPENSSL_3.4.0'
not found", from systemd's own shared library wanting a newer OpenSSL than
the bundled one provides). subprocess.Popen/run inherit the parent's
environment by default, so every external program launched (xdg-open,
flatpak, systemctl, loginctl, a plain Artisan binary) picks this up.
PyInstaller's bootloader preserves whatever LD_LIBRARY_PATH existed before
it overrode it as LD_LIBRARY_PATH_ORIG (only set if one existed at all) --
restore that, or drop the variable entirely if there was none.
"""
from __future__ import annotations

import os
import sys


def external_subprocess_env() -> dict[str, str] | None:
    """Returns None (meaning "use the parent's environment, unmodified")
    when not running frozen, so this has zero effect on a from-source run."""
    if not getattr(sys, "frozen", False):
        return None
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env
