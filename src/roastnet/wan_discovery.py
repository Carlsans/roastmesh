"""Internet-wide peer auto-discovery -- lan_discovery's LAN broadcast,
extended to the whole internet, as easy as joining a BitTorrent swarm: no
ticket-pasting, no bootstrap node of roastnet's own to run or configure.

Every roastnet node that opts in announces itself, via the real public
BitTorrent Mainline DHT (roastnet.dht), under one fixed made-up info-hash
(`SWARM_INFO_HASH`) shared by every roastnet node everywhere -- exactly
like every user of one specific torrent is a peer of every other user of
that torrent. Any other opted-in node looking up that same info-hash finds
our public address, and we exchange the same "hello" datagram
(roastnet.hello) LAN discovery uses, just unicast instead of broadcast.
Once a hello is exchanged, everything after that -- the QUIC handshake,
signature verification, quota checks -- is identical to a LAN-discovered
or manually-pasted peer; see net.sync_with_peer.

Opt-in, unlike LAN discovery: a LAN broadcast never leaves the local
network, but announcing on the public DHT makes a node's public IP address
(and the fact that it's running roastnet) visible to anyone else looking
at that swarm -- a meaningfully bigger exposure, worth a conscious choice
rather than an on-by-default one. See net.serve's `enable_wan_discovery`
and cli.py's `--wan-discovery` flag.

NAT note: unlike LAN discovery, this only works if a "hello" datagram can
actually reach the node from an address it never itself contacted -- true
for most home routers (their NAT state accepts inbound packets to the
public port for some idle window after any outbound packet), but not for
"symmetric" NATs. Nodes behind those simply won't be reachable this way;
they still work fine over LAN discovery, manual tickets, and (for the
resulting QUIC connection, once a ticket IS in hand) Iroh's own
relay/hole-punch fallback -- this module never sees or needs to solve
that part.
"""
from __future__ import annotations

import asyncio
import hashlib
import socket
import time
from collections.abc import Awaitable, Callable

from roastnet.dht import Addr, DhtClient
from roastnet.hello import decode_hello, encode_hello

WAN_PORT = 41890
DHT_LOOKUP_INTERVAL_S = 120.0
HELLO_RESYNC_S = 300.0  # minimum time between (re-)helloing the same address/peer

SWARM_INFO_HASH = hashlib.sha1(b"roastnet-swarm-v1").digest()

DEFAULT_DHT_BOOTSTRAP: list[Addr] = [
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
]


async def _resolve(bootstrap_nodes: list[Addr]) -> list[Addr]:
    """DNS-resolve the bootstrap hostnames once per lookup round -- their
    IPs aren't guaranteed stable, and resolving fresh each time is cheap
    next to the lookup itself. A host that fails to resolve (offline DNS,
    transient failure) is skipped rather than aborting the whole round."""
    loop = asyncio.get_running_loop()
    resolved = []
    for host, port in bootstrap_nodes:
        try:
            ip = (await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM))[0][4][0]
        except OSError:
            continue
        resolved.append((ip, port))
    return resolved


async def run_wan_discovery(
    own_pubkey_hex: str,
    own_ticket: str,
    on_peer_discovered: Callable[[str, str], Awaitable[None]],
    *,
    port: int = WAN_PORT,
    lookup_interval_s: float = DHT_LOOKUP_INTERVAL_S,
    hello_resync_s: float = HELLO_RESYNC_S,
    bootstrap_nodes: list[Addr] | None = None,
    info_hash: bytes = SWARM_INFO_HASH,
) -> None:
    """Announce on the public DHT and react to other roastnet nodes found
    there, until cancelled. `on_peer_discovered(pubkey, ticket)` is
    scheduled as a task per newly-seen peer, debounced by `hello_resync_s`
    same as lan_discovery.run_beacon.

    `bootstrap_nodes`/`port`/interval params are overridable so tests can
    point this at a fake in-process DHT instead of the real public one.
    """
    bootstrap_nodes = bootstrap_nodes if bootstrap_nodes is not None else DEFAULT_DHT_BOOTSTRAP
    own_id = hashlib.sha1(bytes.fromhex(own_pubkey_hex)).digest()
    client = await DhtClient.bind(port=port, own_id=own_id)

    last_helloed: dict[Addr, float] = {}
    last_seen_pubkey: dict[str, float] = {}

    def _maybe_hello(addr: Addr) -> None:
        now = time.monotonic()
        if now - last_helloed.get(addr, 0.0) < hello_resync_s:
            return
        last_helloed[addr] = now
        client.send_datagram(encode_hello(own_pubkey_hex, own_ticket), addr)

    def _on_foreign(data: bytes, addr) -> None:
        decoded = decode_hello(data)
        if decoded is None:
            return
        pubkey, ticket = decoded
        if pubkey == own_pubkey_hex:
            return
        # Reciprocate immediately: whoever reached us first might not yet
        # know about us (their own DHT lookup may not have found our
        # address yet even though theirs found ours) -- a direct hello
        # back closes the loop without waiting for their next lookup round.
        _maybe_hello(addr)
        now = time.monotonic()
        if now - last_seen_pubkey.get(pubkey, 0.0) < hello_resync_s:
            return
        last_seen_pubkey[pubkey] = now
        asyncio.create_task(on_peer_discovered(pubkey, ticket))

    client.on_foreign_datagram = _on_foreign

    try:
        while True:
            try:
                resolved = await _resolve(bootstrap_nodes)
                addrs = await client.discover_and_announce_peers(info_hash, resolved) if resolved else set()
            except Exception:  # noqa: BLE001 -- a bad DHT round shouldn't kill serve()
                addrs = set()
            for addr in addrs:
                _maybe_hello(addr)
            await asyncio.sleep(lookup_interval_s)
    finally:
        client.close()
