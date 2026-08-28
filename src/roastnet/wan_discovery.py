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

from roastnet.dht import Addr, DhtClient, LookupStats, load_node_cache, save_node_cache
from roastnet.hello import decode_hello, encode_hello

WAN_PORT = 41890
DHT_LOOKUP_INTERVAL_S = 120.0
HELLO_RESYNC_S = 60.0  # minimum time between (re-)helloing the same address/peer
HELLO_RETRIES = (0.0, 2.0, 6.0)  # see _hello_with_retries

SWARM_INFO_HASH = hashlib.sha1(b"roastnet-swarm-v1").digest()

# Measured live, not copied from a tutorial: of the traditionally-cited
# routers, only transmissionbt and libtorrent still answer. BitTorrent Inc's
# `router.bittorrent.com` and `router.utorrent.com` resolve but never reply,
# and `router.bitcomet.com` no longer resolves at all. They are kept (last,
# cheap when dead) only in case they come back; the two live ones plus the
# persisted node cache are what actually bootstrap a lookup.
DEFAULT_DHT_BOOTSTRAP: list[Addr] = [
    ("dht.transmissionbt.com", 6881),
    ("dht.libtorrent.org", 25401),
    ("router.bittorrent.com", 6881),
    ("router.utorrent.com", 6881),
]


def default_node_cache_path():
    from pathlib import Path
    return Path.home() / ".local" / "share" / "roastnet" / "dht_nodes.json"


async def _resolve(bootstrap_nodes: list[Addr]) -> list[Addr]:
    """DNS-resolve the bootstrap hostnames to **IPv4** once per lookup round.

    `family=AF_INET` is not optional. BEP 5's compact address format is
    IPv4-only (4-byte addresses, `socket.inet_aton`), and DhtClient binds an
    IPv4 socket -- but `getaddrinfo` with no family returns AAAA records first
    on any IPv6-preferring host, and taking result [0] then hands an IPv6
    address to an IPv4 socket. Every query silently goes nowhere.

    Found on a real dual-stack host: `node doctor` there resolved
    dht.transmissionbt.com to 2001:41d0:203:4cca:5:: and reported "the DHT is
    unreachable from this network", while a raw IPv4 UDP probe to the very
    same router from the very same machine got an immediate reply. Nothing was
    blocked; roastnet was dialling the wrong address family. IPv6 is common on
    consumer ISPs, so this failed completely for an unknown share of users
    while looking exactly like a firewall problem.

    A host that fails to resolve (no A record, offline DNS, transient failure)
    is skipped rather than aborting the whole round.
    """
    loop = asyncio.get_running_loop()
    resolved = []
    for host, port in bootstrap_nodes:
        try:
            infos = await loop.getaddrinfo(host, port, family=socket.AF_INET,
                                            type=socket.SOCK_DGRAM)
        except OSError:
            continue
        if infos:
            resolved.append((infos[0][4][0], port))
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
    node_cache_path=None,
    on_round: Callable[[object], None] | None = None,
) -> None:
    """Announce on the public DHT and react to other roastnet nodes found
    there, until cancelled. `on_peer_discovered(pubkey, ticket)` is
    scheduled as a task per newly-seen peer, debounced by `hello_resync_s`
    same as lan_discovery.run_beacon.

    `bootstrap_nodes`/`port`/interval params are overridable so tests can
    point this at a fake in-process DHT instead of the real public one.
    `on_round` receives each round's LookupStats (used by the logs and by
    `roastnet net doctor`) -- without it a failing round is invisible.
    """
    bootstrap_nodes = bootstrap_nodes if bootstrap_nodes is not None else DEFAULT_DHT_BOOTSTRAP
    cache_path = node_cache_path if node_cache_path is not None else default_node_cache_path()
    own_id = hashlib.sha1(bytes.fromhex(own_pubkey_hex)).digest()
    client = await DhtClient.bind(port=port, own_id=own_id)
    node_cache = load_node_cache(cache_path)

    last_helloed: dict[Addr, float] = {}
    last_seen_pubkey: dict[str, float] = {}

    def _maybe_hello(addr: Addr, *, retry: bool = True) -> None:
        now = time.monotonic()
        if now - last_helloed.get(addr, 0.0) < hello_resync_s:
            return
        last_helloed[addr] = now
        if retry:
            asyncio.create_task(_hello_with_retries(addr))
            return
        try:
            client.send_datagram(encode_hello(own_pubkey_hex, own_ticket), addr)
        except OSError:
            pass

    async def _hello_with_retries(addr: Addr) -> None:
        """Send the first hello more than once.

        The rendezvous datagram goes to an address this node has never
        contacted, so a restricted-cone NAT drops it until the peer's own
        outbound hello opens the pinhole -- which is precisely why both sides
        sending, repeatedly, is what makes the punch land. Previously a single
        lost packet meant five minutes of silence, because the send was
        recorded before it was even attempted.

        Retries stop the moment that address answers. Only *unsolicited* first
        contact retries: a reply is sent once (see `retry=False` below),
        because a peer we just heard from has demonstrably got a path to us,
        and answering a retransmission with another retransmission turns one
        lost packet into an exponential hello storm -- which it did, firing
        five duplicate discoveries (and so five redundant syncs) for a single
        peer before this was split apart."""
        payload = encode_hello(own_pubkey_hex, own_ticket)
        for delay in HELLO_RETRIES:
            if delay:
                await asyncio.sleep(delay)
            if addr in _acked:
                return
            try:
                client.send_datagram(payload, addr)
            except OSError:
                return

    _acked: set[Addr] = set()

    def _on_foreign(data: bytes, addr) -> None:
        decoded = decode_hello(data)
        if decoded is None:
            return
        pubkey, ticket = decoded
        if pubkey == own_pubkey_hex:
            return
        _acked.add(addr)  # heard from them -- no need to keep retransmitting
        # Reciprocate immediately: whoever reached us first might not yet
        # know about us (their own DHT lookup may not have found our
        # address yet even though theirs found ours) -- a direct hello
        # back closes the loop without waiting for their next lookup round.
        _maybe_hello(addr, retry=False)
        now = time.monotonic()
        if now - last_seen_pubkey.get(pubkey, 0.0) < hello_resync_s:
            return
        last_seen_pubkey[pubkey] = now
        asyncio.create_task(on_peer_discovered(pubkey, ticket))

    client.on_foreign_datagram = _on_foreign

    try:
        while True:
            stats = LookupStats()
            try:
                resolved = await _resolve(bootstrap_nodes)
                seeds = list(dict.fromkeys([*resolved, *node_cache]))
                addrs = await client.discover_and_announce_peers(
                    info_hash, seeds, seed_ids=dict(node_cache), stats=stats,
                ) if seeds else set()
            except Exception as exc:  # noqa: BLE001 -- a bad DHT round shouldn't kill serve()
                # Previously swallowed silently, which is how a permanently
                # broken feature stayed invisible through several releases.
                print(f"wan: DHT round failed: {exc!r}", flush=True)
                addrs = set()
            else:
                node_cache.update(dict(stats.live_nodes))
                save_node_cache(cache_path, node_cache)
                print(f"wan: {stats.summary()}", flush=True)
            if on_round is not None:
                on_round(stats)
            for addr in addrs:
                _maybe_hello(addr)
            await asyncio.sleep(lookup_interval_s)
    finally:
        client.close()
