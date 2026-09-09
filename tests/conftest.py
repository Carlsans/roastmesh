"""Shared pytest setup.

The Windows event loop policy has to be installed before pytest-asyncio
creates its first loop, the same way roastmesh.cli installs it before its first
asyncio.run(). Without it the DHT tests fail on Windows for the reason
documented in roastmesh.asyncio_policy -- which is where this was first caught.

Every GUI test also constructs a real RoastmeshApp, which auto-starts a real
`node serve`. Left alone that node beacons on the real LAN port and announces
itself on the real public BitTorrent DHT, with a throwaway identity that
disappears when the test ends -- so running the suite quietly filled this
machine's peers.json with hundreds of dead peers (606 of the 876 there when
this was found) and handed the same junk to every other user via the DHT.
ROASTMESH_DISCOVERY_OFFLINE stops that at the source, for the whole session
and every subprocess it spawns. See net._discovery_is_offline.
"""
import atexit
import os
import shutil
import subprocess
import time

from roastmesh import asyncio_policy


def _pick_free_display_number() -> int:
    for n in range(99, 150):
        if not os.path.exists(f"/tmp/.X{n}-lock"):
            return n
    return 199


def _force_isolated_display() -> None:
    """Force every test that builds a real Tk widget onto a dedicated
    virtual display -- never this machine's real one, no matter which
    test file does it or how.

    Confirmed as a real, repeated incident, not a hypothetical: several
    tests build a live Tk root directly in THIS process (test_chart.py,
    parts of test_widgets.py) gated only by a `_has_display()` check that
    treats "DISPLAY is already set" as "safe to proceed" -- on a machine
    whose own desktop session sets a real DISPLAY (this one does, under
    niri), that sent real, visible windows straight to the developer's
    actual screen mid-test-run. test_gui.py's own fix (always route its
    subprocess through xvfb-run, regardless of the inherited DISPLAY) does
    NOT cover these: they never spawn a subprocess, they call tk.Tk()
    right here. Overriding DISPLAY for this whole pytest process, before
    any test module runs, is the only fix that covers every current and
    future in-process Tk usage at once.
    """
    if os.environ.get("ROASTMESH_TEST_REAL_DISPLAY"):
        return  # explicit opt-out, e.g. a deliberate one-off visual check
    if not shutil.which("Xvfb"):
        return
    display_num = _pick_free_display_number()
    proc = subprocess.Popen(
        ["Xvfb", f":{display_num}", "-screen", "0", "1920x1080x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if os.path.exists(f"/tmp/.X11-unix/X{display_num}"):
            break
        time.sleep(0.1)
    os.environ["DISPLAY"] = f":{display_num}"
    # Same reasoning as test_gui.py's own hardening: a Wayland-aware bit of
    # the stack finding WAYLAND_DISPLAY in its environment is a plausible
    # way to still reach the real compositor even with DISPLAY correctly
    # pointed at an isolated Xvfb X11 server.
    os.environ.pop("WAYLAND_DISPLAY", None)
    os.environ["GDK_BACKEND"] = "x11"
    atexit.register(proc.terminate)


_force_isolated_display()

os.environ.setdefault("ROASTMESH_DISCOVERY_OFFLINE", "1")
# Every GUI test builds RoastmeshApp with a throwaway HOME (no config), which
# is exactly the first-run condition -- without this the modal setup wizard
# would appear and block on wait_window. See RoastmeshApp.__init__.
os.environ.setdefault("ROASTMESH_SKIP_WIZARD", "1")
# The GUI checks GitHub for a newer release shortly after launch (a subprocess
# `roastmesh update --check`). Under test that would be a real network call and
# a spawned process for every RoastmeshApp built -- skip it. See
# RoastmeshApp._start_update_check.
os.environ.setdefault("ROASTMESH_SKIP_UPDATE_CHECK", "1")
# The Devices tab refreshes its paired-device list on a timer (a subprocess
# `device list --json`, which itself does a brief real LAN listen for the
# online flag unless told not to) as long as the tab exists. Every GUI test
# that builds a RoastmeshApp would otherwise start that timer and its
# network probe too -- skip it, same reasoning as ROASTMESH_SKIP_UPDATE_CHECK
# just above. See gui/app.py's DevicesTab.
os.environ.setdefault("ROASTMESH_SKIP_DEVICE_SYNC", "1")

asyncio_policy.apply()
