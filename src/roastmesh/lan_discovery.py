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
from dataclasses import dataclass

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
        # The always-on discovery beacon only ever cares about pubkey/ticket
        # -- pairing/code/hostname exist for discover_pairing_beacons below,
        # not this one, and a pairing-mode hello from a device also running
        # regular discovery is harmless noise here, not an error.
        pubkey, ticket, _pairing, _code, _hostname = decoded
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


@dataclass
class PairingCandidate:
    pubkey: str
    ticket: str
    code: str | None       # the other side's session code, for a human to cross-check
    hostname: str | None   # the other side's advertised device label


async def discover_pairing_beacons(
    own_pubkey_hex: str,
    own_ticket: str,
    *,
    code: str,
    hostname: str,
    port: int = BEACON_PORT,
    interval_s: float = 1.0,
    listen_s: float = 5.0,
) -> list[PairingCandidate]:
    """Broadcast our own pairing-mode hello and collect every other one
    seen within `listen_s`, deduped by pubkey -- the LAN half of
    device_sync.pair_over_lan.

    Deliberately a separate, time-bounded function rather than a mode of
    run_beacon: pairing is a brief, human-attended moment (someone is
    standing at both screens right now), not a background/forever loop, and
    piling a second exit condition onto run_beacon's `while True` would have
    made the always-on discovery path -- which must never stop on its own --
    harder to reason about for no benefit. It does still reuse run_beacon's
    actual socket setup (_join_multicast, _announce, the same multicast
    group and per-interface send) rather than re-deriving any of it: a
    second, subtly different UDP setup here is exactly the kind of drift
    that made plain LAN discovery flaky before _announce/_join_multicast
    were written the way they are (see their own docstrings), and this
    binds its own socket with SO_REUSEPORT (like run_beacon) specifically so
    it can run *alongside* the always-on beacon on the very same port
    without disturbing it in either direction.

    A faster default interval than run_beacon's (1s vs 5s): pairing is a
    short window a human is actively waiting on, where run_beacon's 5s
    default is tuned for a beacon meant to run for hours unnoticed.
    """
    loop = asyncio.get_running_loop()
    candidates: dict[str, PairingCandidate] = {}

    class _PairingBeaconProtocol(asyncio.DatagramProtocol):
        def error_received(self, exc: Exception) -> None:
            print(f"lan: pairing socket error: {exc!r}", flush=True)

        def datagram_received(self, data: bytes, addr) -> None:
            decoded = decode_hello(data)
            if decoded is None:
                return
            pubkey, ticket, pairing, peer_code, peer_hostname = decoded
            if not pairing or pubkey == own_pubkey_hex:
                return  # a plain (non-pairing) beacon, or our own broadcast looping back
            candidates[pubkey] = PairingCandidate(pubkey, ticket, peer_code, peer_hostname)

    sock = udp_socket(port)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    _join_multicast(sock)

    transport, _ = await loop.create_datagram_endpoint(_PairingBeaconProtocol, sock=sock)
    payload = encode_hello(own_pubkey_hex, own_ticket, pairing=True, code=code, hostname=hostname)
    try:
        elapsed = 0.0
        while elapsed < listen_s:
            _announce(sock, transport, payload, port)
            await asyncio.sleep(interval_s)
            elapsed += interval_s
    finally:
        transport.close()
    return list(candidates.values())


async def probe_reachable_devices(*, port: int = BEACON_PORT, duration_s: float = 1.5) -> dict[str, str]:
    """Passively listen for *regular* (non-pairing) discovery beacons for a
    short, bounded window and return {pubkey: ticket} for everyone heard --
    a best-effort "who's reachable on this LAN right now" check.

    Used two ways: `device list`'s online flag (is a paired device's pubkey
    in the result?), and `device sync`'s actual reconciliation (devices.json
    stores only a pubkey/name/platform -- the human already verified via
    SAS, never an address -- so this is how a one-shot `roastmesh device
    sync` invocation finds a ticket to dial a paired device with at all).

    Deliberately does NOT broadcast anything of its own (unlike run_beacon):
    this is a passive listen for a one-shot command, not a node trying to
    be found. Only as good as whoever else happens to already be beaconing
    within this window -- a real, reachable device that isn't currently
    running with LAN discovery enabled, or whose beacon just didn't land in
    this short a listen, is simply absent from the result, not "offline".
    """
    loop = asyncio.get_running_loop()
    seen: dict[str, str] = {}

    class _ProbeProtocol(asyncio.DatagramProtocol):
        def error_received(self, exc: Exception) -> None:
            print(f"lan: probe socket error: {exc!r}", flush=True)

        def datagram_received(self, data: bytes, addr) -> None:
            decoded = decode_hello(data)
            if decoded is None:
                return
            pubkey, ticket, pairing, _code, _hostname = decoded
            if not pairing:  # a regular beacon, not another device mid-pairing
                seen[pubkey] = ticket

    sock = udp_socket(port)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    _join_multicast(sock)
    transport, _ = await loop.create_datagram_endpoint(_ProbeProtocol, sock=sock)
    try:
        await asyncio.sleep(duration_s)
    finally:
        transport.close()
    return seen


def _join_multicast(sock: socket.socket) -> None:
    """Listen to the beacon group on every interface we can find.

    Per interface, not once globally: a multicast join is scoped to one
    interface, and joining only on the routing table's favourite is how the
    broadcast version of this ended up talking exclusively to a VPN tunnel.

    Called on every announce, not once at startup. Interfaces come and go --
    wifi associates after the app starts, a VPN connects, a cable is plugged in
    -- and a group joined only at startup is never joined on any of them. A
    repeat join of a group we already hold is refused by the kernel and
    ignored here, which makes re-running this the cheapest way to stay current.
    Syncthing gets the same effect by letting its reader fail and be restarted.
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
    _join_multicast(sock)
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
