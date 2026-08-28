"""A minimal BitTorrent Mainline DHT (BEP 5) client -- just enough to
`get_peers`/`announce_peer` against the real, already-running, public
BitTorrent DHT (entered via the same well-known routers real BitTorrent
clients use -- though of the traditionally-cited ones only
dht.transmissionbt.com and dht.libtorrent.org still answer; see
wan_discovery.DEFAULT_DHT_BOOTSTRAP).

Why piggyback on BitTorrent's DHT rather than run our own: it already
exists, is already huge and reliable, and needs zero infrastructure of
roastmesh's own to operate or keep alive -- there is no "roastmesh tracker"
anyone has to run. Every roastmesh node announces itself under one fixed,
made-up info-hash (wan_discovery.SWARM_INFO_HASH); any other roastmesh node
looking up that same info-hash finds it. This is exactly the mechanism a
"trackerless" torrent uses to find peers, borrowed for peer discovery
instead of file discovery -- confirmed working against the real public DHT
(dht.transmissionbt.com replied to a real `ping` sent from this project's
own dev sandbox during development).

Deliberately NOT a full DHT node: we only ever originate get_peers/
announce_peer/find_node queries, and never route other people's lookups.
What we do keep is a flat cache of nodes that answered (`load_node_cache`),
not real k-buckets -- enough to start the next lookup near the target
instead of at a bootstrap router, which matters because most of the
well-known routers are dead or rate-limited. We do answer incoming
`ping`, both because it's trivial and because it makes us a slightly
better-behaved participant in someone else's routing table. Anything else
addressed to us is silently ignored -- acceptable for a lightweight client
that only needs its own two operations to work, not to help scale the
wider network.

Security note, same shape as lan_discovery's: nothing here is a trust
mechanism. A DHT-returned address is just a hint of "something might be
listening here" -- the actual roastmesh "hello" handshake (wan_discovery)
and, after that, the QUIC handshake / signature verification / quota
checks in net.sync_with_peer are what anything found this way still has to
go through.
"""
from __future__ import annotations

import asyncio
import json
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

Addr = tuple[str, int]

# Kademlia parameters. `K` is the replication factor: a value announced for a
# target is stored on the K nodes whose IDs are XOR-closest to it, so a lookup
# that does not reach those K nodes finds nothing, no matter how many other
# nodes it asks. `ALPHA` is how many queries are kept in flight per round.
# Both are the values BEP 5 / the Kademlia paper specify.
K = 8
# Kademlia's paper value for ALPHA is 3. We use more because a large share of
# public DHT nodes simply never answer (measured live: 25/34 was a *good*
# round, and cold lookups routinely see half the batch time out), and a round
# spent on three dead nodes is a wasted round. Widening the batch is what makes
# convergence reliable rather than luck-of-the-draw.
ALPHA = 6
MAX_ROUNDS = 24

_UNREACHABLY_FAR = 1 << 200  # sorts after any real 160-bit XOR distance


def load_node_cache(path) -> dict[Addr, bytes]:
    """Live DHT nodes learned by previous lookups, as {(ip, port): node_id}.

    Not an optimisation -- a correctness requirement. Only two of the
    well-known bootstrap routers still answer at all (measured: BitTorrent
    Inc's `router.bittorrent.com` and `router.utorrent.com` resolve but never
    reply), and the survivors rate-limit per source IP, so two cold lookups
    run back to back from one machine can leave the second with almost no
    seeds. Reproduced exactly that: a lookup starved to 4 queried nodes and
    stopped 2^158 from the target. Warm nodes make a lookup independent of
    whether a router feels like answering today."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    cache: dict[Addr, bytes] = {}
    for item in raw if isinstance(raw, list) else []:
        try:
            node_id = bytes.fromhex(item["id"])
            if len(node_id) == 20:
                cache[(str(item["ip"]), int(item["port"]))] = node_id
        except (KeyError, TypeError, ValueError):
            continue
    return cache


def save_node_cache(path, nodes: dict[Addr, bytes], *, limit: int = 400) -> None:
    """Best-effort persist; never raises into the discovery loop."""
    items = [{"ip": ip, "port": port, "id": node_id.hex()}
             for (ip, port), node_id in list(nodes.items())[:limit]]
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(items), encoding="utf-8")
    except OSError:
        pass


def udp_socket(port: int) -> socket.socket:
    """A UDP socket that keeps receiving after a peer refuses a datagram.

    Windows-critical. On Windows, when a datagram provokes an ICMP Port
    Unreachable, the *next* `recvfrom` on that socket fails with
    WSAECONNRESET -- and asyncio's default Proactor transport reports that to
    `error_received()` and then stops reading altogether. The socket goes
    permanently deaf, silently.

    A DHT talks to dead nodes constantly by design: two of the well-known
    bootstrap routers resolve but never answer, and a *good* lookup round here
    was measured at 25 replies out of 34. So this fires within seconds of the
    first round, and the symptom -- endless "0/N replied" -- is indistinguishable
    from a firewall blocking us, which is the exact misdiagnosis `node doctor`
    exists to prevent.

    SIO_UDP_CONNRESET(False) tells Windows not to surface those resets. The
    constant only exists on Windows, hence the guard; on every other platform
    this is a plain UDP socket.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)  # type: ignore[attr-defined]
        except OSError:
            pass  # not fatal: worst case is the pre-existing Windows behaviour
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.setblocking(False)
    return sock


def distance(a: bytes, b: bytes) -> int:
    """Kademlia's XOR metric over two 20-byte IDs. This is the whole basis of
    the DHT: "closer" means smaller XOR, and a target's values live on the
    nodes closest to it by this measure -- not on whichever nodes happen to
    answer first."""
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


@dataclass
class LookupStats:
    """Why a lookup did or didn't find anything -- surfaced by
    `roastmesh net doctor` and the discovery logs. Without this the failure
    mode is invisible: an announce that reaches no storing node and a lookup
    that converges nowhere both just look like "0 peers"."""

    rounds: int = 0
    queried: int = 0
    replied: int = 0
    announced: int = 0
    no_token: int = 0
    peers_found: int = 0
    closest_bits: int | None = None  # log2 of the closest XOR distance reached
    seeds_used: int = 0
    live_nodes: list[tuple[Addr, bytes]] = field(default_factory=list)

    def summary(self) -> str:
        closest = "none" if self.closest_bits is None else f"2^{self.closest_bits}"
        return (f"{self.rounds} rounds, {self.replied}/{self.queried} replied, "
                f"closest {closest}, announced to {self.announced} "
                f"({self.no_token} gave no token), {self.peers_found} peer(s)")


def bencode(obj) -> bytes:
    if isinstance(obj, bool):  # bool is an int subclass -- check first
        raise TypeError("bencode does not support bool")
    if isinstance(obj, int):
        return f"i{obj}e".encode("ascii")
    if isinstance(obj, bytes):
        return str(len(obj)).encode("ascii") + b":" + obj
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        return str(len(b)).encode("ascii") + b":" + b
    if isinstance(obj, (list, tuple)):
        return b"l" + b"".join(bencode(item) for item in obj) + b"e"
    if isinstance(obj, dict):
        def key_bytes(k):
            return k if isinstance(k, bytes) else str(k).encode("utf-8")
        items = sorted(obj.items(), key=lambda kv: key_bytes(kv[0]))
        out = b"d"
        for k, v in items:
            out += bencode(key_bytes(k)) + bencode(v)
        return out + b"e"
    raise TypeError(f"bencode: unsupported type {type(obj)}")


def bdecode(data: bytes):
    value, rest = _bdecode_at(data, 0)
    return value


def _bdecode_at(data: bytes, i: int):
    kind = data[i:i + 1]
    if kind == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if kind == b"l":
        i += 1
        items = []
        while data[i:i + 1] != b"e":
            item, i = _bdecode_at(data, i)
            items.append(item)
        return items, i + 1
    if kind == b"d":
        i += 1
        result = {}
        while data[i:i + 1] != b"e":
            key, i = _bdecode_at(data, i)
            value, i = _bdecode_at(data, i)
            result[key] = value
        return result, i + 1
    if kind.isdigit():
        colon = data.index(b":", i)
        length = int(data[i:colon])
        start = colon + 1
        return data[start:start + length], start + length
    raise ValueError(f"bdecode: unexpected byte {kind!r} at offset {i}")


def encode_compact_addr(addr: Addr) -> bytes:
    ip, port = addr
    return socket.inet_aton(ip) + struct.pack(">H", port)


def decode_compact_peers(blob: bytes) -> list[Addr]:
    peers = []
    for i in range(0, len(blob) - 5, 6):
        ip = socket.inet_ntoa(blob[i:i + 4])
        port = struct.unpack(">H", blob[i + 4:i + 6])[0]
        peers.append((ip, port))
    return peers


def decode_compact_nodes(blob: bytes) -> list[tuple[bytes, Addr]]:
    nodes = []
    for i in range(0, len(blob) - 25, 26):
        node_id = blob[i:i + 20]
        ip = socket.inet_ntoa(blob[i + 20:i + 24])
        port = struct.unpack(">H", blob[i + 24:i + 26])[0]
        nodes.append((node_id, (ip, port)))
    return nodes


class _DhtProtocol(asyncio.DatagramProtocol):
    def __init__(self, client: "DhtClient") -> None:
        self._client = client
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def error_received(self, exc: Exception) -> None:
        # Never silent. Without this, asyncio's default handler is all there
        # is, and a transport that has stopped reading (see udp_socket) looks
        # identical to a quiet network -- which is how a completely dead DHT
        # could be mistaken for a firewall for as long as anyone cared to look.
        print(f"dht: socket error: {exc!r}", flush=True)

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = bdecode(data)
        except (ValueError, IndexError, KeyError):
            msg = None
        if not isinstance(msg, dict) or b"y" not in msg:
            if self._client.on_foreign_datagram is not None:
                self._client.on_foreign_datagram(data, addr)
            return
        self._client._handle_message(msg, addr)


class DhtClient:
    """One UDP socket, used both to speak BEP 5 DHT and (via
    `on_foreign_datagram`, for anything that doesn't bdecode as a DHT
    message) to carry roastmesh's own unicast "hello" handshake -- the two
    share a port because the DHT is what tells a peer *which* port to send
    its hello to (see wan_discovery)."""

    def __init__(self, transport: asyncio.DatagramTransport, own_id: bytes) -> None:
        self._transport = transport
        self.own_id = own_id
        self.on_foreign_datagram: Callable[[bytes, Addr], None] | None = None
        self._pending: dict[bytes, asyncio.Future] = {}
        self._next_t = 0

    @classmethod
    async def bind(cls, *, port: int, own_id: bytes) -> "DhtClient":
        loop = asyncio.get_running_loop()
        client = cls.__new__(cls)
        client.own_id = own_id
        client.on_foreign_datagram = None
        client._pending = {}
        client._next_t = 0
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _DhtProtocol(client), sock=udp_socket(port),
        )
        client._transport = transport
        return client

    def send_datagram(self, data: bytes, addr: Addr) -> None:
        self._transport.sendto(data, addr)

    def close(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._transport.close()

    def _handle_message(self, msg: dict, addr: Addr) -> None:
        y = msg.get(b"y")
        t = msg.get(b"t")
        if y in (b"r", b"e"):
            fut = self._pending.pop(t, None) if t is not None else None
            if fut is not None and not fut.done():
                fut.set_result(msg.get(b"r") if y == b"r" else None)
            return
        if y == b"q" and msg.get(b"q") == b"ping":
            reply = {b"t": t or b"", b"y": b"r", b"r": {b"id": self.own_id}}
            try:
                self.send_datagram(bencode(reply), addr)
            except OSError:
                pass
        # any other incoming query (find_node/get_peers/announce_peer) is
        # silently ignored -- we don't serve the routing table, only use it.

    async def _query(self, addr: Addr, q: str, args: dict, *, timeout: float) -> dict | None:
        self._next_t = (self._next_t + 1) % 65536
        t = struct.pack(">H", self._next_t)
        message = {b"t": t, b"y": b"q", b"q": q.encode("ascii"), b"a": args}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[t] = fut
        try:
            self.send_datagram(bencode(message), addr)
        except OSError:
            self._pending.pop(t, None)
            return None
        try:
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        finally:
            self._pending.pop(t, None)

    async def ping(self, addr: Addr, *, timeout: float = 4.0) -> dict | None:
        return await self._query(addr, "ping", {b"id": self.own_id}, timeout=timeout)

    async def get_peers(self, addr: Addr, info_hash: bytes, *, timeout: float = 4.0) -> dict | None:
        return await self._query(
            addr, "get_peers", {b"id": self.own_id, b"info_hash": info_hash}, timeout=timeout,
        )

    async def announce_peer(self, addr: Addr, info_hash: bytes, token: bytes, *, timeout: float = 4.0) -> dict | None:
        """Announce under this socket's address.

        `implied_port=1` asks the storing node to use the packet's source port,
        which is the only correct value behind NAT (the external port differs
        from ours). But observed on the real swarm: some nodes store the
        *literal value* of implied_port instead, publishing peers as
        `<ip>:1` -- an address no hello can ever reach. Sending our real local
        port rather than the customary 0 costs nothing and gives those nodes
        something usable; nodes that honour implied_port ignore it, and a
        non-NAT host (a VPS, where this was caught) is then advertised
        correctly either way."""
        local_port = 0
        try:
            local_port = int(self._transport.get_extra_info("sockname")[1])
        except (AttributeError, IndexError, TypeError, ValueError):
            pass
        return await self._query(addr, "announce_peer", {
            b"id": self.own_id, b"info_hash": info_hash, b"port": local_port,
            b"implied_port": 1, b"token": token,
        }, timeout=timeout)

    async def discover_and_announce_peers(
        self, info_hash: bytes, bootstrap_nodes: list[Addr], *,
        k: int = K, alpha: int = ALPHA, max_rounds: int = MAX_ROUNDS,
        timeout: float = 4.0, announce: bool = True,
        seed_ids: dict[Addr, bytes] | None = None,
        stats: LookupStats | None = None,
    ) -> set[Addr]:
        """A real iterative Kademlia lookup: repeatedly query the closest
        not-yet-queried nodes we know of until nothing unqueried can beat the
        `k` closest that answered, then announce to exactly those `k`.

        This has to converge, and the reason is the whole point of the DHT:
        BEP 5 stores a target's peers *only* on the k nodes XOR-closest to
        it. Bootstrap routers are ~2^159 away from any given target (and
        two of the three well-known ones no longer answer at all), so a
        lookup that stops one hop from them queries nodes that hold nothing
        and announces to nodes nobody will ever ask -- which is exactly the
        bug this replaces. Reaching the right neighbourhood takes on the
        order of log(network size) distance-sorted rounds, not one.

        Set `announce=False` for a read-only lookup (used by the live
        round-trip test, and by `net doctor` so diagnosing doesn't publish).
        """
        stats = stats if stats is not None else LookupStats()
        target = int.from_bytes(info_hash, "big")

        # Cached seeds arrive with their IDs already known, so they rank by
        # real distance from round one -- the walk starts near the target
        # instead of ~2^159 away at a router.
        node_ids: dict[Addr, bytes] = dict(seed_ids or {})
        tokens: dict[Addr, bytes] = {}
        queried: set[Addr] = set()
        # Nodes that actually answered. Convergence must be judged on these
        # alone: a cached node's ID is known before it is contacted, so
        # counting merely-known nodes let a stale cache satisfy the "k closest
        # have been queried" test on the very first round and abandon the
        # lookup 2^158 from the target without ever trying a live router.
        responded: set[Addr] = set()
        shortlist: set[Addr] = set(bootstrap_nodes) | set(node_ids)
        found: set[Addr] = set()
        stats.seeds_used = len(shortlist)

        def rank(addr: Addr) -> int:
            node_id = node_ids.get(addr)
            # A seed we've never heard from has no ID yet, so no distance --
            # it still gets queried (round one has nothing else), but it must
            # never displace a node whose real distance we know.
            return _UNREACHABLY_FAR if node_id is None else int.from_bytes(node_id, "big") ^ target

        for _round in range(max_rounds):
            unqueried = [a for a in sorted(shortlist, key=rank) if a not in queried]
            if not unqueried:
                break
            batch = unqueried[:alpha]
            stats.rounds += 1
            replies = await asyncio.gather(
                *(self.get_peers(addr, info_hash, timeout=timeout) for addr in batch),
                return_exceptions=True,
            )
            for addr, resp in zip(batch, replies):
                queried.add(addr)
                stats.queried += 1
                if not isinstance(resp, dict):
                    continue  # timeout, error reply, or a raised exception
                stats.replied += 1
                responded.add(addr)
                node_id = resp.get(b"id")
                if isinstance(node_id, bytes) and len(node_id) == 20:
                    node_ids[addr] = node_id
                token = resp.get(b"token")
                if isinstance(token, bytes):
                    tokens[addr] = token
                for raw in resp.get(b"values") or []:
                    found.update(decode_compact_peers(raw))
                for peer_id, peer_addr in decode_compact_nodes(resp.get(b"nodes") or b""):
                    node_ids.setdefault(peer_addr, peer_id)
                    shortlist.add(peer_addr)

            # Textbook Kademlia termination: stop once the k closest nodes we
            # know of have all been queried. Anything laxer (an "it stopped
            # improving" heuristic) exits early on a lossy network and leaves
            # the walk far from the target -- measured at 2^77 and 2^154 on
            # runs that should have reached ~2^15.
            # Standard Kademlia termination -- stop once the k closest nodes
            # worth considering have all been queried -- with dead nodes
            # evicted from that set. Both halves are load-bearing, and each
            # was learned by watching a real lookup fail:
            #
            #  * Counting nodes that never answered let a stale cache satisfy
            #    the test on round one and abandon the walk 2^158 away,
            #    because the cached entries were closest and all silent.
            #  * Requiring each round to find something *closer* killed cold
            #    lookups instantly, because bootstrap routers return nodes
            #    farther from the target than the routers themselves (measured:
            #    a router at 2^156 handing back eight nodes at 2^158). Progress
            #    through the network is not monotonic; you have to walk the
            #    frontier outward before it turns inward.
            dead = queried - responded
            frontier = sorted(
                (a for a in shortlist if a in node_ids and a not in dead), key=rank,
            )[:k]
            if frontier and all(a in queried for a in frontier):
                break

        # The k closest are the only nodes worth announcing to, and a token is
        # only valid from the node that issued it -- so any of the k closest we
        # haven't actually asked yet must be asked now, or the announce is
        # skipped for want of a token. (Observed: "7 of 8 gave no token",
        # because the final round discovered closer nodes it never queried.)
        # Announce to the k closest nodes that actually *answered*. Ranking
        # merely-known nodes here was a quiet killer: `nodes` blobs are full of
        # stale entries, so the k closest known were mostly dead, produced no
        # token, and the announce silently reached nobody -- measured as
        # "announced to 0 (7 gave no token)" on a lookup that had otherwise
        # converged perfectly to 2^38. A token is only obtainable from a node
        # that replied, so those are the only candidates that were ever real.
        closest = sorted(responded, key=rank)[:k]
        if closest:
            stats.closest_bits = max(rank(closest[0]).bit_length() - 1, 0)
        stats.live_nodes = [(a, node_ids[a]) for a in closest if a in node_ids]

        if announce:
            for addr in closest:
                token = tokens.get(addr)
                if token is None:
                    stats.no_token += 1
                    continue
                if await self.announce_peer(addr, info_hash, token, timeout=timeout) is not None:
                    stats.announced += 1

        found.discard(("0.0.0.0", 0))
        stats.peers_found = len(found)
        return found
