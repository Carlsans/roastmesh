"""dht.py's iterative lookup, against a real in-process DHT swarm.

Why this file exists, and why it is shaped the way it is: the previous
WAN-discovery test replaced the DHT with a stub that answered *every*
`get_peers` with a canned peer list, never read the `info_hash`, and handed
each node the other's address at fixture-build time. It passed for months
while internet discovery had never once worked, because the one thing
discovery must do -- put a value somewhere a stranger's independent lookup
will later look -- was pre-solved by the fixture instead of tested.

So the swarm below is a real (small) Kademlia network: every node has a real
random 160-bit ID, stores announces only when handed a valid token it issued,
and answers `get_peers` with the closest nodes *it* knows plus any values it
is actually holding. Nodes know only a slice of the network, so reaching the
target's neighbourhood genuinely requires iterating and re-sorting by XOR
distance -- exactly what the real algorithm has to do.

The load-bearing test is `test_the_old_shallow_lookup_does_not_converge`: a
fixture a broken implementation can still pass proves nothing.
"""
from __future__ import annotations

import asyncio
import json
import secrets

from roastmesh.dht import (
    K,
    DhtClient,
    LookupStats,
    bdecode,
    bencode,
    bep42_node_id,
    decode_compact_nodes,
    decode_compact_peers,
    distance,
    encode_compact_addr,
    load_node_cache,
    save_node_cache,
)

SWARM_SIZE = 150
NEIGHBOURS_CLOSE = 8   # each node knows its own closest peers ...
NEIGHBOURS_RANDOM = 8  # ... plus long-range links, so greedy routing converges


def _encode_nodes(entries: list[tuple[bytes, tuple[str, int]]]) -> bytes:
    return b"".join(nid + encode_compact_addr(addr) for nid, addr in entries)


class _SwarmNode(asyncio.DatagramProtocol):
    """A minimal but *honest* BEP 5 responder: real ID, real token discipline,
    distance-aware `nodes`, and storage that only ever returns what was
    actually announced to this particular node."""

    def __init__(self, node_id: bytes) -> None:
        self.node_id = node_id
        self.addr: tuple[str, int] | None = None
        self.neighbours: list[tuple[bytes, tuple[str, int]]] = []
        self.stored: dict[bytes, set[tuple[str, int]]] = {}
        self._tokens: dict[tuple[str, int], bytes] = {}
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = bdecode(data)
        except (ValueError, IndexError, KeyError):
            return
        if not isinstance(msg, dict) or msg.get(b"y") != b"q":
            return
        args = msg.get(b"a") or {}
        q = msg.get(b"q")
        reply: dict | None = None

        if q == b"ping":
            reply = {b"id": self.node_id}
        elif q == b"get_peers":
            info_hash = args.get(b"info_hash", b"")
            token = secrets.token_bytes(4)
            self._tokens[addr] = token
            reply = {b"id": self.node_id, b"token": token}
            held = self.stored.get(info_hash)
            if held:
                reply[b"values"] = [encode_compact_addr(p) for p in sorted(held)]
            closest = sorted(self.neighbours, key=lambda n: distance(n[0], info_hash))[:K]
            reply[b"nodes"] = _encode_nodes(closest)
        elif q == b"announce_peer":
            if args.get(b"token") != self._tokens.get(addr):
                return  # bad/absent token -- BEP 5 says drop it
            info_hash = args.get(b"info_hash", b"")
            port = addr[1] if args.get(b"implied_port") else int(args.get(b"port", 0))
            self.stored.setdefault(info_hash, set()).add((addr[0], port))
            reply = {b"id": self.node_id}

        if reply is not None and self.transport is not None:
            self.transport.sendto(bencode({b"t": msg.get(b"t", b""), b"y": b"r", b"r": reply}), addr)


class _SybilNode(asyncio.DatagramProtocol):
    """The live sybil fleet, reproduced: for *whatever* target is queried --
    not a fixed one -- replies with an ID forged to share the target's first
    15 bytes, hands out a token unconditionally, and returns a fabricated
    `values` entry. Accepts (and silently discards) announce_peer, matching
    the observation that the real fleet's "peers" are unrelated addresses
    sharing one port rather than anything actually stored -- see the plan
    doc's live measurement.

    A fixed target would be a strictly weaker test double (real DHT
    implementations don't special-case one info-hash), so the forged ID is
    derived from the info-hash *in the query*, every time.
    """

    def __init__(self, *, fabricated_peer: tuple[str, int]) -> None:
        self.addr: tuple[str, int] | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self._fabricated_peer = fabricated_peer

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = bdecode(data)
        except (ValueError, IndexError, KeyError):
            return
        if not isinstance(msg, dict) or msg.get(b"y") != b"q":
            return
        args = msg.get(b"a") or {}
        q = msg.get(b"q")
        target = args.get(b"info_hash") or args.get(b"target") or b"\x00" * 20
        forged_id = target[:15] + secrets.token_bytes(5)
        if q == b"ping":
            reply = {b"id": forged_id}
        elif q == b"get_peers":
            reply = {
                b"id": forged_id,
                b"token": secrets.token_bytes(4),
                b"values": [encode_compact_addr(self._fabricated_peer)],
            }
        elif q == b"announce_peer":
            reply = {b"id": forged_id}  # accepted, stored nowhere
        else:
            return
        self.transport.sendto(bencode({b"t": msg.get(b"t", b""), b"y": b"r", b"r": reply}), addr)


FABRICATED_SYBIL_PEER = ("175.114.214.10", 55275)  # shape of the live measurement:
                                                     # unrelated IPs sharing one port


class _Swarm:
    def __init__(self) -> None:
        self.nodes: list[_SwarmNode] = []
        self.sybils: list[_SybilNode] = []
        self._transports: list[asyncio.DatagramTransport] = []

    async def start(self, size: int = SWARM_SIZE) -> None:
        loop = asyncio.get_running_loop()
        for _ in range(size):
            node = _SwarmNode(secrets.token_bytes(20))
            transport, _proto = await loop.create_datagram_endpoint(
                lambda n=node: n, local_addr=("127.0.0.1", 0),
            )
            node.addr = transport.get_extra_info("sockname")
            self.nodes.append(node)
            self._transports.append(transport)
        # Small-world wiring: own closest neighbours + random long-range links.
        # Without the random links the graph is a ring and lookups crawl; with
        # only random links there is no gradient to descend.
        for node in self.nodes:
            others = [n for n in self.nodes if n is not node]
            close = sorted(others, key=lambda n: distance(n.node_id, node.node_id))[:NEIGHBOURS_CLOSE]
            far = secrets.SystemRandom().sample(others, min(NEIGHBOURS_RANDOM, len(others)))
            node.neighbours = [(n.node_id, n.addr) for n in {id(x): x for x in close + far}.values()]

    async def add_sybil_fleet(self, count: int, *, fabricated_peer: tuple[str, int] = FABRICATED_SYBIL_PEER) -> None:
        """The sybils are reachable exactly the way the real fleet was: as
        direct seeds (a poisoned node cache, in production -- see the plan
        doc's finding #3), not wired into any honest node's neighbour list.
        They are never returned by an honest node's `nodes` field."""
        loop = asyncio.get_running_loop()
        for _ in range(count):
            node = _SybilNode(fabricated_peer=fabricated_peer)
            transport, _proto = await loop.create_datagram_endpoint(
                lambda n=node: n, local_addr=("127.0.0.1", 0),
            )
            node.addr = transport.get_extra_info("sockname")
            self.sybils.append(node)
            self._transports.append(transport)

    def seeds(self, count: int = 3) -> list[tuple[str, int]]:
        """Entry points, deliberately *not* close to any particular target --
        the same situation a real bootstrap router presents."""
        return [n.addr for n in self.nodes[:count]]

    def seeds_with_sybils(self, count: int = 3) -> list[tuple[str, int]]:
        return [n.addr for n in self.sybils] + self.seeds(count)

    def holders(self, info_hash: bytes) -> list[_SwarmNode]:
        return [n for n in self.nodes if n.stored.get(info_hash)]

    def close(self) -> None:
        for transport in self._transports:
            transport.close()


async def _shallow_lookup(client: DhtClient, info_hash: bytes, seeds, *, max_extra_hops: int = 16):
    """The *previous* algorithm, reproduced verbatim in behaviour: query the
    seeds, follow one flat unsorted hop, announce to whoever answered. Kept
    only so the test below can prove it fails -- see this module's docstring."""
    queried: set = set()
    extra: list = []
    found: set = set()
    announced: list = []

    async def visit(addr):
        queried.add(addr)
        resp = await client.get_peers(addr, info_hash, timeout=2.0)
        if resp is None:
            return
        for raw in resp.get(b"values") or []:
            found.update(decode_compact_peers(raw))
        for _nid, naddr in decode_compact_nodes(resp.get(b"nodes") or b""):
            if naddr not in queried and len(extra) < max_extra_hops:
                extra.append(naddr)
        token = resp.get(b"token")
        if token is not None:
            await client.announce_peer(addr, info_hash, token, timeout=2.0)
            announced.append(addr)

    for addr in list(seeds):
        await visit(addr)
    for addr in list(extra):
        if addr not in queried:
            await visit(addr)
    return found, announced


# --- unit: the distance metric and its use ---------------------------------

def test_distance_is_xor_and_orders_correctly() -> None:
    assert distance(b"\x00" * 20, b"\x00" * 20) == 0
    assert distance(b"\x00" * 20, b"\x00" * 19 + b"\x01") == 1
    target = b"\x00" * 20
    near, far = b"\x00" * 19 + b"\x01", b"\xff" * 20
    assert distance(near, target) < distance(far, target)


def test_node_cache_roundtrips_and_survives_garbage(tmp_path) -> None:
    # load_node_cache re-validates every entry (is_martian + BEP 42) -- see
    # test_poisoned_node_cache_entries_are_dropped_on_load below -- so a
    # roundtrip needs real BEP-42-conforming IDs, not arbitrary bytes.
    nodes = {
        ("1.2.3.4", 6881): bep42_node_id("1.2.3.4", b"seed-a"),
        ("5.6.7.8", 6889): bep42_node_id("5.6.7.8", b"seed-b"),
    }
    path = tmp_path / "nodes.json"
    save_node_cache(path, nodes)
    assert load_node_cache(path) == nodes

    path.write_text("not json{{{")
    assert load_node_cache(path) == {}          # never raises into the loop
    path.write_text('[{"ip": "1.2.3.4"}, {"id": "zz"}]')
    assert load_node_cache(path) == {}          # malformed entries skipped


def test_missing_node_cache_is_empty_not_an_error(tmp_path) -> None:
    assert load_node_cache(tmp_path / "absent.json") == {}


def test_poisoned_node_cache_entries_are_dropped_on_load(tmp_path) -> None:
    """The old dht_nodes.json is exactly the sybil fleet's memory: measured
    live at 36 cached nodes, 26 of them BEP 42 invalid, so every restart
    re-seeded a lookup one hop inside the trap. load_node_cache must not
    trust a cache file just because it parses as JSON -- every entry gets
    re-validated the same way a live search would reject it."""
    path = tmp_path / "nodes.json"
    good_ip = "203.0.113.9"
    path.write_text(json.dumps([
        # A genuine, BEP-42-conforming entry -- survives.
        {"ip": good_ip, "port": 6881, "id": bep42_node_id(good_ip, b"honest").hex()},
        # Forged: a random ID does not verify against its claimed IP.
        {"ip": "198.51.100.7", "port": 6881, "id": secrets.token_bytes(20).hex()},
        # Martian source address -- never a legitimate remote node.
        {"ip": "127.0.0.1", "port": 6881, "id": bep42_node_id("127.0.0.1", b"loop").hex()},
        {"ip": "198.51.100.8", "port": 0, "id": secrets.token_bytes(20).hex()},
    ]))
    loaded = load_node_cache(path)
    assert loaded == {(good_ip, 6881): bep42_node_id(good_ip, b"honest")}


# --- the real thing: announce here, find it from an independent lookup -----

async def test_announce_is_retrievable_by_an_independent_lookup() -> None:
    """The assertion that did not exist before, and the only one that actually
    proves discovery works: one node announces, a *different* node -- separate
    socket, separate ID, no shared state, told nothing but the same seeds --
    looks the info-hash up and gets the announcer's address back."""
    swarm = _Swarm()
    await swarm.start()
    info_hash = secrets.token_bytes(20)
    announcer = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    seeker = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        stats = LookupStats()
        await announcer.discover_and_announce_peers(
            info_hash, swarm.seeds(), timeout=2.0, stats=stats,
        )
        assert stats.announced > 0, f"announced to nobody: {stats.summary()}"

        announced_port = announcer._transport.get_extra_info("sockname")[1]
        found = await seeker.discover_and_announce_peers(
            info_hash, swarm.seeds(), timeout=2.0, announce=False,
        )
        assert ("127.0.0.1", announced_port) in found, (
            f"independent lookup did not find the announce; got {sorted(found)}"
        )
    finally:
        announcer.close()
        seeker.close()
        swarm.close()


async def test_the_old_shallow_lookup_does_not_converge() -> None:
    """Teeth check: a fixture the replaced algorithm can also pass would prove
    nothing, so assert directly on the defect.

    The old lookup queried the seeds plus one flat, *distance-blind* hop and
    announced to whoever answered, so its announce targets are essentially a
    random sample of the network. The new one only ever announces to the k
    closest. Comparing the worst distance each is willing to announce to is
    deterministic and scale-free -- unlike "did a stranger find it", which in a
    60-node swarm comes down to whether 19 random picks happened to overlap the
    closest 8 (in the real ~10^7-node DHT that overlap is vanishingly rare,
    which is exactly why this bug was fatal in production and invisible here).
    """
    swarm = _Swarm()
    await swarm.start()
    info_hash = secrets.token_bytes(20)
    by_addr = {n.addr: n.node_id for n in swarm.nodes}
    old = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    new = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        _found, old_targets = await _shallow_lookup(old, info_hash, swarm.seeds())
        assert old_targets, "shallow lookup announced nowhere at all"

        stats = LookupStats()
        await new.discover_and_announce_peers(info_hash, swarm.seeds(), timeout=2.0, stats=stats)
        assert stats.announced > 0, stats.summary()

        worst_old = max(distance(by_addr[a], info_hash) for a in old_targets if a in by_addr)
        worst_new = max(distance(nid, info_hash) for _a, nid in stats.live_nodes)
        assert worst_new < worst_old, (
            f"new lookup is no more selective than the old one "
            f"(2^{worst_new.bit_length() - 1} vs 2^{worst_old.bit_length() - 1})"
        )

        # Precision, not raw count: in a small swarm a scattergun of ~19
        # announces can incidentally cover the whole neighbourhood. What
        # separates the two is that the new lookup announces *only* to close
        # nodes, while the old one mostly announces to distant ones -- and it
        # is those wasted announces that vanish into a real 10^7-node network.
        true_closest = sorted(swarm.nodes, key=lambda n: distance(n.node_id, info_hash))[:K]
        band = distance(true_closest[-1].node_id, info_hash)
        old_hits = [a for a in old_targets if a in by_addr and distance(by_addr[a], info_hash) <= band]
        old_precision = len(old_hits) / len(old_targets)
        new_precision = sum(
            1 for _a, nid in stats.live_nodes if distance(nid, info_hash) <= band
        ) / len(stats.live_nodes)
        assert new_precision > old_precision, (
            f"new lookup announces no more selectively than the old "
            f"({new_precision:.0%} of targets near the goal vs {old_precision:.0%})"
        )
    finally:
        old.close()
        new.close()
        swarm.close()


async def test_lookup_converges_into_the_target_neighbourhood() -> None:
    """Convergence, stated as the property that actually matters: the nodes we
    end up announcing to are the ones closest to the target, not whichever
    answered first."""
    swarm = _Swarm()
    await swarm.start()
    info_hash = secrets.token_bytes(20)
    client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        stats = LookupStats()
        await client.discover_and_announce_peers(info_hash, swarm.seeds(), timeout=2.0, stats=stats)

        true_closest = sorted(swarm.nodes, key=lambda n: distance(n.node_id, info_hash))[:K]
        worst_true = distance(true_closest[-1].node_id, info_hash)
        reached = [nid for _addr, nid in stats.live_nodes]
        assert reached, stats.summary()
        # At least half of what we settled on must be genuinely in the k-closest
        # band -- a distance-blind walk lands there essentially never.
        in_band = sum(1 for nid in reached if distance(nid, info_hash) <= worst_true)
        assert in_band >= K // 2, f"only {in_band}/{len(reached)} were near the target: {stats.summary()}"
        assert stats.rounds > 1, "converged without iterating -- fixture too easy"
    finally:
        client.close()
        swarm.close()


async def test_lookup_survives_nodes_that_never_answer_and_give_no_token() -> None:
    """The real network's actual behaviour: two of three well-known routers
    never reply, and the survivor answers `get_peers` without a token. Neither
    may stop the lookup from converging and announcing."""
    swarm = _Swarm()
    await swarm.start()
    info_hash = secrets.token_bytes(20)

    dead = ("127.0.0.1", 9)  # discard port: never answers
    client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        stats = LookupStats()
        # 3s, not 1s. The dead seed has to time out for this test to mean
        # anything, so the budget is real wall-clock time -- and 1s proved too
        # tight on a loaded Windows CI runner driving 150 in-process nodes,
        # failing there while passing on the previous run of the same code.
        # The point of the test is that a dead seed doesn't stop the lookup,
        # not how fast the rest of the swarm answers.
        await client.discover_and_announce_peers(
            info_hash, [dead, *swarm.seeds()], timeout=3.0, stats=stats,
        )
        assert stats.announced > 0, f"dead seed broke the lookup: {stats.summary()}"
        assert stats.queried > stats.replied  # the dead one was genuinely tried
    finally:
        client.close()
        swarm.close()


# --- the sybil fleet: the measured bug, reproduced in-process -------------

async def test_sybil_fleet_captures_the_unfiltered_lookup() -> None:
    """The bug, exactly as measured against the real public DHT (see the
    plan doc): a fleet that forges IDs sharing the first 15 bytes of
    *whatever* target is queried, and hands back fabricated `values`, fully
    captures a lookup that has no insertion-time filtering. The old shallow
    lookup (`_shallow_lookup`, kept purely to prove the point -- see this
    file's docstring) has no distance discrimination at all, so every sybil
    it reaches answers and gets announced to.
    """
    swarm = _Swarm()
    await swarm.start()
    await swarm.add_sybil_fleet(12)
    info_hash = secrets.token_bytes(20)
    client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        seeds = swarm.seeds_with_sybils()
        found, announced = await _shallow_lookup(client, info_hash, seeds)

        sybil_addrs = {n.addr for n in swarm.sybils}
        assert sybil_addrs, "fixture bug: no sybils in the fleet"
        assert sybil_addrs.issubset(set(announced)), (
            "the unfiltered lookup did not announce to every sybil it was seeded with -- "
            f"missing {sybil_addrs - set(announced)}"
        )
        assert FABRICATED_SYBIL_PEER in found, "fabricated peer was not handed back"
    finally:
        client.close()
        swarm.close()


async def test_search_rejects_the_sybil_fleet_and_converges_on_honest_holders() -> None:
    """The fix, proven the same way the bug was measured: seed a lookup with
    both the sybil fleet and a few genuine entry points, announce through
    the new `Search`-backed `discover_and_announce_peers`, and require that
    (a) the announce set contains no sybil, (b) the filters actually fired
    (a vacuous pass -- nothing to reject -- would prove nothing), (c) an
    *independent* second client, seeded the same way, retrieves the real
    announced address, and (d) the fabricated sybil peer never leaks into
    that result.
    """
    swarm = _Swarm()
    await swarm.start()
    await swarm.add_sybil_fleet(12)
    info_hash = secrets.token_bytes(20)
    seeds = swarm.seeds_with_sybils()

    announcer = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    seeker = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        stats = LookupStats()
        await announcer.discover_and_announce_peers(info_hash, seeds, timeout=2.0, stats=stats)
        assert stats.announced > 0, f"filtered lookup announced nowhere: {stats.summary()}"

        sybil_addrs = {n.addr for n in swarm.sybils}
        announced_addrs = {addr for addr, _node_id in stats.live_nodes}
        assert not (announced_addrs & sybil_addrs), (
            f"announced to a sybil: {announced_addrs & sybil_addrs}"
        )
        # The fleet's forged IDs share 15 bytes with the target, landing
        # deep inside IMPOSSIBLE_PROXIMITY_THRESHOLD -- if this is ever 0,
        # the fleet was never actually offered to the filters and the test
        # above proves nothing.
        assert stats.rejected_impossible_proximity >= len(swarm.sybils), stats.summary()

        announced_port = announcer._transport.get_extra_info("sockname")[1]
        found = await seeker.discover_and_announce_peers(info_hash, seeds, timeout=2.0, announce=False)
        assert ("127.0.0.1", announced_port) in found, (
            "independent lookup did not find the real announce; "
            f"got {sorted(found)}\n  announce: {stats.summary()}"
        )
        assert FABRICATED_SYBIL_PEER not in found, "fabricated sybil peer leaked into the result"
    finally:
        announcer.close()
        seeker.close()
        swarm.close()
