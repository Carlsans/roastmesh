# Kept in sync with pyproject.toml's version at each release -- read by
# index/ingest.py's version-gated refresh to know when the local index
# was last brought up to date (see refresh_known_sources).
__version__ = "0.5.1"

import asyncio
import sys

if sys.platform == "win32":  # pragma: no cover - exercised only on Windows CI
    # Windows' default asyncio loop (Proactor) permanently stops reading a UDP
    # socket after one error. That is fatal for a DHT, which provokes errors
    # constantly by design: sending to a node that is gone draws an ICMP port
    # unreachable, which Windows reports as WSAECONNRESET on the *next* read,
    # and Proactor's read loop then does not reschedule itself. The socket goes
    # deaf and discovery silently dies.
    #
    # dht.udp_socket() also asks Windows not to report those resets at all
    # (SIO_UDP_CONNRESET), which is the documented remedy -- but measured on CI,
    # that is not sufficient on its own: the ioctl succeeds and the reset still
    # arrives, collapsing a lookup to "2/18 replied" and 2^158 from the target.
    # The selector loop's datagram transport keeps reading after an error, so it
    # survives what the proactor does not.
    #
    # The cost of the selector loop on Windows is asyncio subprocess support,
    # which this project does not use anywhere: net.py offloads blocking work
    # with asyncio.to_thread, and gui/runner.py runs the CLI through plain
    # subprocess on a worker thread. Checked before choosing this, not assumed.
    #
    # Set at import so every entry point agrees -- the CLI, the CLI as spawned
    # by the GUI, and the test suite, which is where the failure was caught.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
