"""LAN peer auto-discovery: a lightweight UDP broadcast beacon so two
roastmesh nodes on the same local network find each other with zero manual
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
attempt; it can't make roastmesh trust unverified content.
"""
from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Awaitable, Callable

from roastmesh.dht import udp_socket
from roastmesh.hello import decode_hello, encode_hello
from roastmesh.interfaces import local_interfaces

BEACON_PORT = 41888

# Our own administratively-scoped group (239.192.0.0/14), deliberately *not*
# BEP 14's 239.192.152.143:6771. Reusing that would deliver roastmesh hellos to
# every BitTorrent client on the LAN, which cannot parse them -- rude at best,
# and indistinguishable from someone probing them at worst. The mechanism is
# borrowed from Transmission's LPD; the address space is not.
MULTICAST_GROUP = "239.192.152.144"

# Link-local only. A beacon has no business leaving the switch it was sent on,
# and a TTL of 1 guarantees it cannot.
MULTICAST_TTL = 1
BEACON_INTERVAL_S = 5.0
# Minimum time between auto-syncs with the same discovered peer. A real bug
# found by benchmarking this against an actually-live LAN peer (not just
# two processes on one host, which -- confirmed separately -- sync in
# ~30ms and completely hid this): the sync itself took ~9 seconds of
# mostly-CPU-bound work (Iroh's own connection-establishment cost, not
# roastmesh's), not the near-zero cost assumed when this was first set to
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
    _join_multicast(sock)

    transport, _ = await loop.create_datagram_endpoint(
        lambda: _BeaconProtocol(own_pubkey_hex, _handle), sock=sock,
    )

    payload = encode_hello(own_pubkey_hex, own_ticket)
    try:
        while True:
            _announce(sock, transport, payload, port)
            await asyncio.sleep(interval_s)
    finally:
        transport.close()


def _join_multicast(sock: socket.socket) -> None:
    """Listen to the beacon group on every interface we can find.

    Per interface, not once globally: a multicast join is scoped to one
    interface, and joining only on the routing table's favourite is how the
    broadcast version of this ended up talking exclusively to a VPN tunnel.
    """
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)
    except OSError:
        pass
    group = socket.inet_aton(MULTICAST_GROUP)
    for iface in local_interfaces():
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            group + socket.inet_aton(iface.address))
        except OSError:
            # Plenty of interfaces cannot carry multicast (tunnels especially),
            # and one that cannot is not a reason to give up on the others.
            continue


def _announce(sock: socket.socket, transport, payload: bytes, port: int) -> None:
    """Send the beacon out of every interface, not just the default route's.

    The bug this exists for, measured on a Raspberry Pi with a VPN up: a single
    send to 255.255.255.255 was routed onto the tunnel (`ip route get
    255.255.255.255` -> `dev tun0`) and the machine's actual LAN on wlan0 never
    saw it, so LAN discovery found nobody on the one network where it should be
    effortless.

    Note what is *not* here: when interfaces are known we no longer send the
    global broadcast at all. On that same Pi it went nowhere but into the VPN,
    which means handing our pubkey and ticket to whatever sits at the other end
    of the tunnel for no possible benefit.
    """
    interfaces = local_interfaces()
    if not interfaces:
        # Unknown platform. Exactly the old behaviour, which is the right
        # fallback: worse than per-interface, better than silence.
        try:
            transport.sendto(payload, ("255.255.255.255", port))
        except OSError:
            pass
        return

    for iface in interfaces:
        if iface.broadcast:
            try:
                transport.sendto(payload, (iface.broadcast, port))
            except OSError:
                pass
        try:
            # The address form of IP_MULTICAST_IF, which every platform
            # accepts -- Linux also takes an ip_mreqn with an index, Windows
            # does not.
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                            socket.inet_aton(iface.address))
            transport.sendto(payload, (MULTICAST_GROUP, port))
        except OSError:
            pass
