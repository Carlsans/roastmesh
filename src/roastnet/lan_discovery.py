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
import json
import socket
import time
from collections.abc import Awaitable, Callable

BEACON_PORT = 41888
BEACON_INTERVAL_S = 5.0
RESYNC_INTERVAL_S = 60.0  # minimum time between auto-syncs with the same discovered peer


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, own_pubkey_hex: str, handle: Callable[[str, str], None]) -> None:
        self._own_pubkey_hex = own_pubkey_hex
        self._handle = handle

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
            pubkey = msg["pubkey"]
            ticket = msg["ticket"]
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError, TypeError):
            return
        if not isinstance(pubkey, str) or not isinstance(ticket, str):
            return
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

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", port))
    sock.setblocking(False)

    transport, _ = await loop.create_datagram_endpoint(
        lambda: _BeaconProtocol(own_pubkey_hex, _handle), sock=sock,
    )

    payload = json.dumps({"v": 1, "pubkey": own_pubkey_hex, "ticket": own_ticket}).encode("utf-8")
    try:
        while True:
            try:
                transport.sendto(payload, ("255.255.255.255", port))
            except OSError:
                pass  # no broadcast-capable interface right now; keep trying next interval
            await asyncio.sleep(interval_s)
    finally:
        transport.close()
