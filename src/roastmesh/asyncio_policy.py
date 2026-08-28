"""The asyncio event loop policy roastmesh needs on Windows.

Windows' default loop (Proactor) permanently stops reading a UDP socket after
one error. That is fatal for a DHT, which provokes errors constantly by design:
sending to a node that has gone away draws an ICMP port unreachable, which
Windows reports as WSAECONNRESET on the *next* read, and Proactor's read loop
does not reschedule itself. The socket goes deaf and discovery dies silently.

dht.udp_socket() also asks Windows not to report those resets at all
(SIO_UDP_CONNRESET), which is the documented remedy -- but measured on CI, that
is not sufficient on its own: the ioctl succeeds and the reset still arrives,
collapsing a lookup to "2/18 replied" and 2^158 from the target. The selector
loop's datagram transport keeps reading after an error, so it survives what the
proactor does not.

Its one cost on Windows is asyncio subprocess support, which this project uses
nowhere: net.py offloads blocking work with asyncio.to_thread, and
gui/runner.py runs the CLI through plain subprocess on a worker thread.

Why this lives in its own module rather than in roastmesh/__init__.py, where it
started: importing asyncio from the package root made *every* entry point pay
for it, including the GUI, which never runs an event loop. In the frozen
Windows build that import pulled in asyncio.windows_events and its `_overlapped`
C extension, which PyInstaller had not bundled precisely because nothing needed
it -- and roastmesh-gui.exe died at startup with "No module named
'_overlapped'". Applying the policy from the modules that actually use asyncio
keeps that cost where it belongs.
"""
from __future__ import annotations

import sys


def apply() -> None:
    """Install the selector event loop policy on Windows. No-op elsewhere.

    Safe to call more than once, and safe to call before any loop exists --
    which is the point: it has to run before the first asyncio.run().
    """
    if sys.platform != "win32":
        return
    import asyncio  # imported here, not at module scope, for the reason above

    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is not None:
        asyncio.set_event_loop_policy(policy())
