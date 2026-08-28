"""LAN peer auto-discovery: a lightweight UDP broadcast beacon so two
roastnet nodes on the same local network find each other with zero manual
ticket-pasting.

Not part of ARCHITECTURE.md's original Peer Discovery design (well-known
bootstrap nodes + manual entry + gossip, no LAN broadcast) -- added on top
because Iroh's stable Python bindings expose no discovery mechanism yet
(confirmed by introspecting the installed `iroh` package: no Discovery/mDNS
API anywhere in it), and a same-LAN setup is exactly the case a
bootstrap-node-less network most needs a fallback for.

Deliberately not a trust mechanism: a received beacon is just a hint of
"try this ticket" -- everything after that (the QUIC handshake, signature
verification, quota checks) is exactly what a manually-pasted ticket goes
through in net.sync_with_peer. A forged beacon can waste a connection
attempt; it can't make roastnet trust unverified content.
"""
from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Awaitable, Callable

from roastnet.dht import udp_socket
from roastnet.hello import decode_hello, encode_hello

BEACON_PORT = 41888
BEACON_INTERVAL_S = 5.0
# Minimum time between auto-syncs with the same discovered peer. A real bug
# found by benchmarking this against an actually-live LAN peer (not just
# two processes on one host, which -- confirmed separately -- sync in
# ~30ms and completely hid this): the sync itself took ~9 seconds of
# mostly-CPU-bound work (Iroh's own connection-establishment cost, not
# roastnet's), not the near-zero cost assumed when this was first set to
# 60s. At 60s, one always-on peer on the LAN means roughly 9/60 = 15% of a
# core, continuously, forever -- with two (also observed live), more like
# 30% -- easily enough to keep a laptop's fan cycling even while nobody's
# touching the app. 900s (15 minutes) cuts that to about 1% while a LAN
# peer still shows up automatically well within a coffee break, and a user
# who wants it sooner can always hit "Sync" manually.
RESYNC_INTERVAL_S = 900.0


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, own_pubkey_hex: str, handle: Callable[[str, str], None]) -> None:
        self._own_pubkey_hex = own_pubkey_hex
        self._handle = handle

    def error_received(self, exc: Exception) -> None:
        # Surfaced rather than swallowed, for the same reason as dht.py's:
        # a transport that has quietly stopped reading is indistinguishable
        # from a network with no peers on it.
        print(f"lan: socket error: {exc!r}", flush=True)

    def datagram_received(self, data: bytes, addr) -> None:
        decoded = decode_hello(data)
        if decoded is None:
            return
        pubkey, ticket = decoded
        if pubkey == self._own_pubkey_hex:
            return  # broadcasts loop back to the sender on the same host
        self._handle(pubkey, ticket)


async def run_beacon(
    own_pubkey_hex: str,
    own_ticket: str,
    on_peer_discovered: Callable[[str, str], Awaitable[None]],
    *,
    port: int = BEACON_PORT,
    interval_s: float = BEACON_INTERVAL_S,
    resync_interval_s: float = RESYNC_INTERVAL_S,
) -> None:
    """Broadcast our own ticket periodically and react to others' beacons,
    until cancelled. `on_peer_discovered(pubkey, ticket)` is scheduled as a
    task for each newly-seen peer, debounced per `resync_interval_s` so a
    peer's repeated beacons don't each trigger a fresh sync.

    `port`/`interval_s` are parameters rather than hardcoded specifically so
    tests can run two beacons against each other on one host without
    needing two separate machines.
    """
    loop = asyncio.get_running_loop()
    last_seen: dict[str, float] = {}

    def _handle(pubkey: str, ticket: str) -> None:
        now = time.monotonic()
        if now - last_seen.get(pubkey, 0.0) < resync_interval_s:
            return
        last_seen[pubkey] = now
        asyncio.create_task(on_peer_discovered(pubkey, ticket))

    # udp_socket() applies SO_REUSEADDR, the bind, and -- on Windows -- the
    # SIO_UDP_CONNRESET ioctl that stops one ICMP "port unreachable" from
    # making the socket permanently deaf. See its docstring in dht.py; a
    # beacon broadcasting to a LAN where nothing is listening provokes exactly
    # that.
    sock = udp_socket(port)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    transport, _ = await loop.create_datagram_endpoint(
        lambda: _BeaconProtocol(own_pubkey_hex, _handle), sock=sock,
    )

    payload = encode_hello(own_pubkey_hex, own_ticket)
    try:
        while True:
            try:
                transport.sendto(payload, ("255.255.255.255", port))
            except OSError:
                pass  # no broadcast-capable interface right now; keep trying next interval
            await asyncio.sleep(interval_s)
    finally:
        transport.close()
