"""Internet-wide peer auto-discovery -- lan_discovery's LAN broadcast,
extended to the whole internet, as easy as joining a BitTorrent swarm: no
ticket-pasting, no bootstrap node of roastmesh's own to run or configure.

Every roastmesh node that opts in announces itself, via the real public
BitTorrent Mainline DHT (roastmesh.dht), under one fixed made-up info-hash
(`SWARM_INFO_HASH`) shared by every roastmesh node everywhere -- exactly
like every user of one specific torrent is a peer of every other user of
that torrent. Any other opted-in node looking up that same info-hash finds
our public address, and we exchange the same "hello" datagram
(roastmesh.hello) LAN discovery uses, just unicast instead of broadcast.
Once a hello is exchanged, everything after that -- the QUIC handshake,
signature verification, quota checks -- is identical to a LAN-discovered
or manually-pasted peer; see net.sync_with_peer.

Opt-in, unlike LAN discovery: a LAN broadcast never leaves the local
network, but announcing on the public DHT makes a node's public IP address
(and the fact that it's running roastmesh) visible to anyone else looking
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
import json
import random
import shutil
import socket
import time
from collections.abc import Awaitable, Callable

from roastmesh.dht import (
    IP_VOTE_QUORUM,
    _bep42_exempt,
    Addr,
    DhtClient,
    LookupStats,
    bep42_node_id,
    bep42_valid,
    load_node_cache,
    save_node_cache,
)
from roastmesh.hello import decode_hello, encode_hello
from roastmesh.interfaces import is_private_address
from roastmesh.port_mapping import map_udp_port, release as release_port_mapping
from roastmesh.paths import data_dir

WAN_PORT = 41890
DHT_LOOKUP_INTERVAL_S = 120.0
HELLO_RESYNC_S = 60.0  # minimum time between (re-)helloing the same address/peer
HELLO_RETRIES = (0.0, 2.0, 6.0)  # see _hello_with_retries
RETRY_INTERVAL_S = 20.0  # after a round that announced to nobody; see run_wan_discovery

# Announce cadence. Storing nodes drop an announced peer after ~32 minutes
# (dht.c's expire_storage), so re-announcing every 15 keeps us continuously
# present with a wide margin, and the jitter stops every node on the network
# re-announcing in lockstep.
ANNOUNCE_INTERVAL_S = 900.0
ANNOUNCE_JITTER = 0.1

# Transmission gates its first announce on a warm routing table
# (tr-dht.cc: is_ready) and so do we. Announcing from a cold table is how a
# node ends up publishing itself to whichever handful of strangers answered
# first, rather than to the k nodes actually closest to the swarm.
WARM_GOOD_NODES = 8

# Don't publish ourselves from a lookup that never reached the swarm's
# neighbourhood. Transmission gates on its routing table because its searches
# seed from it; ours seed from the routers and the persisted state, so the
# equivalent -- and more direct -- condition is that the walk actually got
# close. Measured on the live DHT, a healthy lookup lands at 2^138-2^143 and a
# starved one stops in the 2^150s, so this sits just above the healthy band.
ANNOUNCE_MAX_BITS = 150

# How long to wait before asking a router that just said no again. Routers that
# do not speak PCP or NAT-PMP never will, so this is about not hammering them.
MAPPING_RETRY_S = 900.0

# A permanent mapping has no expiry to renew before, so this is only a
# periodic re-check that it is still there.
PERMANENT_RECHECK_S = 3600.0

# Consecutive announcing rounds whose read-back failed before we say the
# configured port has stopped working. One failure is a bad round; two in a row
# with a port configured means the forward is gone.
STALE_PORT_ROUNDS = 2

# Bootstrap drip, from tr-dht.cc's bootstrap_interval: ping the first few
# quickly, then slow down. The routers rate-limit per source IP, and firing
# all of them on every round is what earned us that limiting.
BOOTSTRAP_FAST_COUNT = 8
BOOTSTRAP_FAST_INTERVAL_S = 2.0
BOOTSTRAP_SLOW_INTERVAL_S = 15.0

# BEP 42: "Since a single node can not be trusted, there should be some
# mechanism to determine whether or not the node has a correct understanding
# of its external IP". The quorum, the rotation that stops a stale address
# winning forever, and the hysteresis that stops it flapping all live in
# dht.IpVoter; IP_VOTE_QUORUM is imported from there.

# Deliberately still "roastnet", the project's former name. This string is not
# a label -- it is the rendezvous point every node looks itself up under, so
# changing it splits the network in half: renamed nodes would announce to one
# neighbourhood of the DHT while everyone still on an older build looks in
# another, and the two would never meet again. It is opaque to users, so there
# is nothing to gain by churning it and a working swarm to lose.
SWARM_INFO_HASH = hashlib.sha1(b"roastnet-swarm-v1").digest()

# Measured live, not copied from a tutorial: of the traditionally-cited
# routers, only transmissionbt and libtorrent still answer. BitTorrent Inc's
# `router.bittorrent.com` and `router.utorrent.com` resolve but never reply,
# and `router.bitcomet.com` no longer resolves at all. They are kept (last,
# cheap when dead) only in case they come back; the two live ones plus the
# persisted node cache are what actually bootstrap a lookup.
# Literal addresses for the routers below, used only when DNS cannot answer.
# Not a micro-optimisation: measured on a real deployment (a Raspberry Pi
# reached over Tailscale) where getaddrinfo failed for *every* public name and
# outbound port 53 was refused outright -- while UDP to the DHT itself worked
# perfectly, replying to a ping on the first try. A node like that has no way
# into the network at all once its state file is empty, and it reports exactly
# the same "no bootstrap router answered" as a machine with no internet, so the
# diagnosis points at the wrong thing entirely. Transmission ships a
# dht.bootstrap file of literal addresses for the same reason.
#
# A fallback, never a preference: DNS wins whenever it works (these addresses
# will go stale eventually), and the persisted state file supersedes both after
# one successful round.
DHT_BOOTSTRAP_FALLBACK_IPS: dict[str, str] = {
    "dht.transmissionbt.com": "87.98.162.88",
    "dht.libtorrent.org": "185.157.221.247",
}

DEFAULT_DHT_BOOTSTRAP: list[Addr] = [
    ("dht.transmissionbt.com", 6881),
    ("dht.libtorrent.org", 25401),
    ("router.bittorrent.com", 6881),
    ("router.utorrent.com", 6881),
]


def default_state_path():
    """Deliberately a new filename.

    The old `dht_nodes.json` cannot be reused, and not for a format reason:
    it was written from `stats.live_nodes`, i.e. the k nodes closest to the
    swarm hash that answered -- which under the sybil capture this rewrite
    fixes meant it filled up with the attacking fleet and re-seeded every
    subsequent lookup straight back into it. Measured on this machine before
    the fix: 36 cached nodes, 26 of them failing BEP 42. A poisoned cache
    that survives restarts is worse than no cache, so the old file is
    abandoned rather than migrated.
    """
    return data_dir() / "dht_state.json"


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
    blocked; roastmesh was dialling the wrong address family. IPv6 is common on
    consumer ISPs, so this failed completely for an unknown share of users
    while looking exactly like a firewall problem.

    A host that fails to resolve (no A record, offline DNS, transient failure)
    falls back to a literal address if we ship one for it (see
    DHT_BOOTSTRAP_FALLBACK_IPS), and is otherwise skipped rather than aborting
    the whole round.
    """
    return [addr for _host, addr in await _resolve_named(bootstrap_nodes) if addr is not None]


async def _resolve_named(bootstrap_nodes: list[Addr]) -> list[tuple[str, Addr | None]]:
    """`_resolve`, but keeping each result beside the host it came from.

    `node doctor` needs the pairing to report which router did what, and
    zipping the two lists is wrong the moment one fails to resolve: every
    later row then reports the wrong host's result, which is worse than no
    report at all on the one machine where several fail.
    """
    loop = asyncio.get_running_loop()
    out: list[tuple[str, Addr | None]] = []
    for host, port in bootstrap_nodes:
        addr: Addr | None = None
        try:
            infos = await loop.getaddrinfo(host, port, family=socket.AF_INET,
                                            type=socket.SOCK_DGRAM)
        except OSError:
            infos = []
        if infos:
            addr = (infos[0][4][0], port)
        elif host in DHT_BOOTSTRAP_FALLBACK_IPS:
            addr = (DHT_BOOTSTRAP_FALLBACK_IPS[host], port)
        out.append((host, addr))
    return out


def external_address(client: DhtClient) -> tuple[Addr | None, str, int]:
    """Our public address, and what the spread of answers says about the NAT.

    BEP 42 asks every node to echo the querier's address back in the reply's
    top-level `ip` field, and enough of them do: 17 independent votes on a
    single measured lookup. Two nodes reporting two different *ports* for one
    socket is the signature of a symmetric NAT or CGNAT -- worth surfacing
    loudly, because it means an unsolicited hello can never arrive and no
    amount of DHT correctness will change that. A single node is never
    enough; the spec is explicit that one node cannot be trusted here.
    """
    votes = client.ip_votes
    total = sum(votes.values())
    best = client.external_address
    if best is None:
        return None, "unknown", total
    # Ports across the current round and the previous one: a tally that has
    # just rotated is empty, and reading the verdict from it alone would
    # report a symmetric node as consistent for the next few votes.
    ports = client.external_ports_seen
    return best, ("symmetric" if len(ports) > 1 else "consistent"), total


def needs_public_port(nat: str, readback: bool | None, public_port: int | None) -> bool:
    """Whether this node should be told to configure a forwarded port.

    Two independent symptoms, either of which means our published address is
    not one anyone can use: a NAT that hands out a different port per
    destination (so the port we are seen from is meaningless), or an announce
    that we then could not find ourselves. Both are settled facts by the time
    they are reported, not guesses -- and neither can be fixed from inside the
    DHT, which is why this points at configuration instead.
    """
    if public_port is not None:
        # Configured: only a *proven* failure is worth raising. Most rounds do
        # not announce -- that happens every ~15 minutes -- so readback is
        # usually unknown, and treating unknown as broken nags a node whose
        # forward is working perfectly. Observed exactly that on a Pi moments
        # after a stranger had found it and synced over that very port: the
        # NAT is still symmetric, which is *why* the port was configured, so
        # the symmetric test alone can never clear.
        return readback is False
    return nat == "symmetric" or readback is False


def double_nat_verdict(router_external_ip: str | None, external: Addr | None) -> str | None:
    """What the router's own idea of our address says, when we can ask it.

    UPnP is the only one of the three mapping protocols with a "what is my
    public address" call, and the answer is worth more than a confirmation. If
    the router reports a *private* address, the router is itself behind another
    NAT -- carrier-grade NAT, stated positively for the first time rather than
    inferred from a symmetric mapping. No forwarded port can work through that,
    and it is the single most useful thing to be able to tell a user who
    cannot be reached.
    """
    if router_external_ip is None:
        return None
    if is_private_address(router_external_ip):
        return "double-nat"
    if external is None:
        # Nothing to agree *with*. Saying "your router agrees" here was wrong
        # and confidently so -- seen on a node whose DHT tally had not reached
        # a quorum yet, which is exactly when a user most wants a straight
        # answer about their address.
        return "unconfirmed"
    if external[0] != router_external_ip:
        return "disagrees"
    return "agrees"


def diagnostics_payload(client: DhtClient, stats: LookupStats, *, info_hash: bytes,
                        external: Addr | None, nat: str, votes: int, warm: bool,
                        readback: bool | None, addrs, public_port: int | None = None,
                        router_external_ip: str | None = None) -> dict:
    """The single definition of the diagnostics contract.

    Both producers use it -- the `wan-stats:` line `node serve` emits every
    round, and `node doctor --json` -- so the GUI panel cannot be reading one
    shape from one and a different shape from the other.
    """
    target = int.from_bytes(info_hash, "big")
    announce_set = []
    for addr, node_id in stats.live_nodes:
        d = int.from_bytes(node_id, "big") ^ target
        announce_set.append({
            "addr": f"{addr[0]}:{addr[1]}",
            "bits": max(d.bit_length() - 1, 0),
            "bep42": bep42_valid(node_id, addr[0]),
        })
    table = client.routing_table
    good = table.good_nodes()
    return {
        "external_ip": external[0] if external else None,
        "external_port": external[1] if external else None,
        "nat": nat,
        "ip_votes": votes,
        "node_id": client.own_id.hex(),
        "node_id_bep42": bep42_valid(client.own_id, external[0]) if external else None,
        "routing_table": {
            "total": len(table),
            "good": len(good),
            "verified": sum(1 for n in good if bep42_valid(n.id, n.addr[0]) is True),
        },
        "warm": warm,
        "lookup": {
            "rounds": stats.rounds, "queried": stats.queried, "replied": stats.replied,
            "closest_bits": stats.closest_bits, "announced": stats.announced,
            "no_token": stats.no_token, "peers_found": stats.peers_found,
            "rejected_martian": stats.rejected_martian,
            "rejected_impossible_proximity": stats.rejected_impossible_proximity,
            "rejected_bep42": stats.rejected_bep42,
        },
        "announce_set": announce_set,
        "readback": readback,
        "public_port": public_port,
        "router_external_ip": router_external_ip,
        "double_nat": double_nat_verdict(router_external_ip, external),
        "needs_public_port": needs_public_port(nat, readback, public_port),
        "peers": sorted(f"{a[0]}:{a[1]}" for a in addrs),
        "swarm_info_hash": info_hash.hex(),
    }


async def _ask_router_external_ip() -> str | None:
    """What the router says our public address is, via UPnP.

    Separate from the mapping because the two are independent questions and
    only one protocol can answer this one. Done once: the answer changes about
    as often as the ISP changes our address, and a multicast search every round
    would be noise on the LAN for nothing.
    """
    from roastmesh import upnp

    def _look() -> str | None:
        igd = upnp.discover()
        return upnp.get_external_ip(igd) if igd is not None else None

    try:
        return await asyncio.wait_for(asyncio.to_thread(_look), 15.0)
    except Exception:  # noqa: BLE001 -- no router is an ordinary answer
        return None


async def _renew_mapping(internal_port: int):
    """Ask the router for a forwarded port, tolerating every way that fails."""
    try:
        return await map_udp_port(internal_port)
    except Exception:  # noqa: BLE001 -- a router is not a dependency
        return None


async def _warn_port_went_stale(port: int) -> None:
    """Say so when a forward that used to work has stopped.

    A leased port is not permanent -- a VPN reconnecting is enough to change
    it -- and the failure is silent by nature: we keep announcing a port that
    no longer reaches us, and the only symptom is that nobody arrives. Naming
    the likely cause beats leaving the user to notice an absence.
    """
    message = (f"wan: port {port} was announced but a fresh lookup could not find this "
               "node twice running -- the forward has probably gone away.")
    current = await _pia_forwarded_port()
    if current is not None and current != port:
        message += (f" Private Internet Access now reports port {current}; "
                    "its forwarded port changes when it reconnects.")
    print(message, flush=True)


async def _pia_forwarded_port() -> int | None:
    """PIA's current forwarded port, if PIA is even installed.

    Deliberately narrow and entirely optional: `piactl` is one specific VPN's
    tool, this is the one place where naming it saves the user a real search,
    and a machine without it simply gets the generic message.
    """
    if shutil.which("piactl") is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "piactl", "get", "portforward",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        return int(out.decode().strip())
    except (OSError, ValueError, asyncio.TimeoutError):
        return None


async def run_wan_discovery(
    own_pubkey_hex: str,
    own_ticket: str,
    on_peer_discovered: Callable[[str, str], Awaitable[None]],
    *,
    port: int = WAN_PORT,
    lookup_interval_s: float = DHT_LOOKUP_INTERVAL_S,
    hello_resync_s: float = HELLO_RESYNC_S,
    retry_interval_s: float = RETRY_INTERVAL_S,
    bootstrap_nodes: list[Addr] | None = None,
    info_hash: bytes = SWARM_INFO_HASH,
    node_cache_path=None,
    on_round: Callable[[object], None] | None = None,
    allow_loopback: bool = False,
    public_port: int | None = None,
    auto_port: bool = False,
    debug: bool = False,
) -> None:
    """Announce on the public DHT and react to other roastmesh nodes found
    there, until cancelled. `on_peer_discovered(pubkey, ticket)` is
    scheduled as a task per newly-seen peer, debounced by `hello_resync_s`
    same as lan_discovery.run_beacon.

    `bootstrap_nodes`/`port`/interval params are overridable so tests can
    point this at a fake in-process DHT instead of the real public one --
    `allow_loopback` is there for the same reason. It must stay False in
    production: `dht.is_martian` rejects 127.x precisely so that a hostile
    node cannot put loopback addresses in a `nodes` blob and aim our queries
    at whatever is listening on this machine.
    `auto_port` asks the router for a forwarded port (PCP/NAT-PMP, see
    port_mapping) and uses whatever it grants, renewing before the lease runs
    out. What the router claims is never taken on trust: the port is announced
    and then confirmed by the read-back, and a mapping that does not survive
    that is reported as failing like any other.

    `public_port` is the port other nodes can reach this machine on, when a
    router or VPN forwards one to us. Without it we announce with BEP 5's
    `implied_port`, which asks storing nodes to record the source port they
    saw -- correct behind an ordinary NAT, and exactly wrong behind a port
    forward, where the reachable port and the outbound source port are
    different numbers.

    `on_round` receives each round's LookupStats (used by the logs and by
    `roastmesh net doctor`) -- without it a failing round is invisible.
    """
    if debug:
        print("wan: debug logging enabled -- wan-stats emitted every lookup round", flush=True)
    bootstrap_nodes = bootstrap_nodes if bootstrap_nodes is not None else DEFAULT_DHT_BOOTSTRAP
    state_path = node_cache_path if node_cache_path is not None else default_state_path()
    own_seed = bytes.fromhex(own_pubkey_hex)
    # Provisional: a conforming ID needs an external IP we have not learned
    # yet, so we start with this and adopt the real one once the votes agree
    # (see _maybe_adopt_node_id).
    own_id = hashlib.sha1(own_seed).digest()
    client = await DhtClient.bind(port=port, own_id=own_id, allow_loopback=allow_loopback)
    state = load_node_cache(state_path)
    failed_rounds = 0

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
                # Only *retries* are cancelled by an ack. The first send is
                # unconditional, and that distinction is load-bearing: this
                # runs as a task, so a hello from the peer can land between
                # `_maybe_hello` recording the attempt and this coroutine
                # first running. Checking `_acked` before the initial send
                # then skipped it entirely -- and the reciprocal send in
                # `_on_foreign` was already suppressed by the `last_helloed`
                # debounce the caller had just set. The result was a node that
                # never introduced itself to a peer whose hello arrived first,
                # so discovery worked in exactly one direction.
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

    adopted_for_ip: str | None = None

    def _maybe_adopt_node_id(external: Addr | None) -> None:
        nonlocal adopted_for_ip
        if external is None or external[0] == adopted_for_ip:
            return
        adopted_for_ip = external[0]
        if bep42_valid(client.own_id, external[0]) is True:
            return
        if client.adopt_node_id(bep42_node_id(external[0], own_seed)):
            print(f"wan: adopted a BEP 42 node id for external ip {external[0]}", flush=True)

    async def _bootstrap_loop() -> None:
        """Feed the routing table from the routers, slowly.

        tr-dht.cc drips its bootstrap queue in rather than firing the whole
        list at once, and the reason applies here with force: the surviving
        routers rate-limit per source IP, and hammering all four on every
        round is what earns that limiting. Stops entirely once the table is
        warm, which after the first successful search it always is.
        """
        added = 0
        queue: list[Addr] = []
        while True:
            if client._transport.is_closing():
                return
            if len(client.routing_table.good_nodes()) >= WARM_GOOD_NODES:
                await asyncio.sleep(BOOTSTRAP_SLOW_INTERVAL_S)
                continue
            if not queue:
                queue = list(dict.fromkeys([*await _resolve(bootstrap_nodes), *state]))
            if not queue:
                await asyncio.sleep(BOOTSTRAP_SLOW_INTERVAL_S)
                continue
            try:
                await client.bootstrap_ping(queue.pop(0))
            except OSError:
                pass
            # Adopt here, not only at the top of a lookup round. Taking up a
            # BEP 42 identity rebuilds the routing table (its bucket boundaries
            # are all relative to the old ID), so it wants to happen while the
            # table is still nearly empty. Rounds are minutes apart, and by the
            # time one comes around there is a warm table to throw away --
            # whereas the routers answering this drip supply the votes within
            # seconds of startup.
            _maybe_adopt_node_id(external_address(client)[0])
            added += 1
            await asyncio.sleep(BOOTSTRAP_FAST_INTERVAL_S if added < BOOTSTRAP_FAST_COUNT
                                else BOOTSTRAP_SLOW_INTERVAL_S)

    async def _seeds() -> list[Addr]:
        return list(dict.fromkeys([*await _resolve(bootstrap_nodes), *state]))

    async def _readback(external: Addr | None) -> bool | None:
        """Ground truth: having announced, can a fresh lookup actually find us?

        Every other signal here can read green while discovery is completely
        broken -- which is exactly how the sybil capture survived several
        releases, with `node doctor` reporting a converged lookup and a
        successful announce into a black hole. This asks the only question
        that actually matters, and it is the check whose absence let all of
        that go unnoticed.
        """
        if external is None:
            return None
        # What we published, which is not always where we were seen from: with
        # a forwarded port the announce carries that port explicitly, so that
        # is the address other nodes hold for us and the one to look for.
        published = (external[0], effective_port) if effective_port is not None else external
        seeds = await _seeds()
        if not seeds:
            return None
        try:
            found = await client.discover_and_announce_peers(
                info_hash, seeds, seed_ids=dict(state), stats=LookupStats(), announce=False,
            )
        except Exception:  # noqa: BLE001 -- a failed probe is "unknown", not a crash
            return None
        return published in found

    bootstrap_task = asyncio.create_task(_bootstrap_loop())
    next_announce_at = 0.0
    # The port we actually publish: what the caller configured, or whatever the
    # router granted under `auto_port`.
    effective_port = public_port
    mapping_renew_at = 0.0
    stale_port_rounds = 0
    # What the router says our public address is, when UPnP could ask it.
    router_external_ip: str | None = None
    asked_router = False
    router_query: asyncio.Task | None = None

    async def _fill_router_ip() -> None:
        nonlocal router_external_ip
        router_external_ip = await _ask_router_external_ip()
    # What the router says our public address is, when UPnP could ask it.
    router_external_ip: str | None = None
    # The last read-back verdict we actually measured, kept across rounds:
    # most rounds do not announce, and forgetting it every time would flip the
    # read-only flag on and off for no reason.
    readback_state: bool | None = None

    try:
        while True:
            stats = LookupStats()
            readback: bool | None = None
            # Ask the router its public address once, whatever the port
            # configuration is. This was gated on the UPnP mapping path, then on
            # auto_port, and both were wrong in the same way: the question is a
            # diagnostic, not part of mapping. A node given an explicit
            # --public-port -- which is exactly what a VPN forward looks like,
            # and the case most likely to be behind a second NAT -- got no
            # verdict at all, while `node doctor` on the same machine printed
            # one, because it always asked.
            if router_external_ip is None and not asked_router:
                asked_router = True
                # Fired off, not awaited. It is a multicast search that waits
                # seconds for an answer -- on a network with no router, the
                # full timeout -- and awaiting it here put that delay in front
                # of the first DHT round, holding up the announce for a
                # diagnostic nobody is waiting on. Caught by a Windows CI run
                # where the absence of any router made the wait maximal.
                router_query = asyncio.create_task(_fill_router_ip())

            if auto_port and time.monotonic() >= mapping_renew_at:
                mapping = await _renew_mapping(port)
                if mapping is not None:
                    if mapping.external_port != effective_port:
                        print(f"wan: router mapped port {mapping.external_port} "
                              f"({mapping.protocol}, {mapping.lifetime_s}s)", flush=True)
                    effective_port = mapping.external_port
                    router_external_ip = mapping.external_ip or router_external_ip
                    # Halfway through the lease, so a renewal that fails still
                    # leaves a working mapping while we retry. A lease of 0 is
                    # a *permanent* mapping -- some routers grant nothing else
                    # -- and renewing that on a 60-second timer would be the
                    # arithmetic doing something the router never asked for.
                    mapping_renew_at = time.monotonic() + (
                        PERMANENT_RECHECK_S if mapping.lifetime_s == 0
                        else max(mapping.lifetime_s / 2, 60.0))
                else:
                    mapping_renew_at = time.monotonic() + MAPPING_RETRY_S

            external, nat, votes = external_address(client)
            _maybe_adopt_node_id(external)
            # Declare ourselves read-only exactly while we know we cannot be
            # found (BEP 43). Costs nothing, and keeps us out of routing tables
            # we would only be dead weight in.
            client.read_only = needs_public_port(nat, readback_state, effective_port)
            warm = len(client.routing_table.good_nodes()) >= WARM_GOOD_NODES
            # "Due" is all the caller can judge up front; whether the lookup
            # actually got near the swarm is only known once it has run, so
            # that half is `announce_if` below. Gating on the routing table
            # instead looked right and was not: measured live, a node whose
            # lookups were converging perfectly to 2^140 sat there for five
            # minutes refusing to announce, because the table it was being
            # judged on is not what those lookups seed from.
            announce = time.monotonic() >= next_announce_at
            addrs: set = set()
            try:
                seeds = await _seeds()
                if seeds:
                    addrs = await client.discover_and_announce_peers(
                        info_hash, seeds, seed_ids=dict(state), stats=stats, announce=announce,
                        announce_if=lambda st: (st.closest_bits is not None
                                                and st.closest_bits <= ANNOUNCE_MAX_BITS),
                        public_port=effective_port,
                    )
            except Exception as exc:  # noqa: BLE001 -- a bad DHT round shouldn't kill serve()
                # Previously swallowed silently, which is how a permanently
                # broken feature stayed invisible through several releases.
                print(f"wan: DHT round failed: {exc!r}", flush=True)
            else:
                # Persist good, *diverse* routing-table nodes -- not the k
                # closest to the swarm hash, which is what the old cache saved
                # and precisely what a sybil fleet controls.
                state.update({n.addr: n.id for n in client.routing_table.good_nodes()})
                save_node_cache(state_path, state)
                if announce and stats.announced > 0:
                    next_announce_at = time.monotonic() + ANNOUNCE_INTERVAL_S * (
                        1.0 + random.uniform(-ANNOUNCE_JITTER, ANNOUNCE_JITTER))
                    # Re-read the address here rather than reusing the one from
                    # the top of the round: the lookup we just ran is usually
                    # what pushed the vote count over the quorum, so the value
                    # captured beforehand is still None on exactly the round
                    # that first announces -- and the check would quietly never
                    # run at all. Measured: announced to 5, read-back "None".
                    external, _nat, _votes = external_address(client)
                    readback = await _readback(external)
                    readback_state = readback
                    if effective_port is not None and readback is False:
                        stale_port_rounds += 1
                        if stale_port_rounds >= STALE_PORT_ROUNDS:
                            await _warn_port_went_stale(effective_port)
                    elif readback is True:
                        stale_port_rounds = 0
                print(f"wan: {stats.summary()}", flush=True)

            external, nat, votes = external_address(client)
            print("wan-stats: " + json.dumps(diagnostics_payload(
                client, stats, info_hash=info_hash, external=external, nat=nat, votes=votes,
                # Recomputed, not the pre-lookup value: the round we just ran is
                # what fills the routing table, so reporting the count from
                # before it makes a healthy node read as permanently cold.
                warm=len(client.routing_table.good_nodes()) >= WARM_GOOD_NODES,
                readback=readback, addrs=addrs, public_port=effective_port,
                router_external_ip=router_external_ip)), flush=True)
            if on_round is not None:
                on_round(stats)
            for addr in addrs:
                _maybe_hello(addr)

            # Retry sooner when a round achieved nothing, instead of sitting
            # out the full interval. Measured: a healthy first round makes a
            # brand-new node discoverable by a stranger in under 15 seconds --
            # but a round that reached nobody left the node invisible for the
            # whole 120s until the next one. The average was never the problem;
            # that worst case was. Backs off to the normal interval as soon as
            # a round succeeds, so steady-state traffic is unchanged.
            if stats.replied > 0:
                failed_rounds = 0
                delay = lookup_interval_s
            else:
                failed_rounds += 1
                delay = min(retry_interval_s * (2 ** (failed_rounds - 1)), lookup_interval_s)
            await asyncio.sleep(delay)
    finally:
        # Cancelled, never awaited. Awaiting it here deadlocks: this coroutine
        # is itself usually being cancelled at this point, so the await raises
        # CancelledError, and catching that to "clean up tidily" means the task
        # silently refuses to die -- `node serve` then hangs forever on
        # shutdown. The race that made awaiting tempting (a drip mid-send when
        # the socket closes) is handled in DhtClient.send_datagram instead.
        bootstrap_task.cancel()
        if router_query is not None:
            router_query.cancel()
        # A UPnP mapping can outlive this process -- a router that refuses
        # timed leases hands out a permanent one, and nothing removes it but
        # us. Best effort: a kill or a power cut skips this entirely.
        try:
            await release_port_mapping()
        except Exception:  # noqa: BLE001 -- shutdown is not a place to raise
            pass
        client.close()
