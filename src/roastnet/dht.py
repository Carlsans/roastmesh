"""A minimal BitTorrent Mainline DHT (BEP 5) client -- just enough to
`get_peers`/`announce_peer` against the real, already-running, public
BitTorrent DHT (bootstrapped via the same well-known routers real
BitTorrent clients use: router.bittorrent.com and friends).

Why piggyback on BitTorrent's DHT rather than run our own: it already
exists, is already huge and reliable, and needs zero infrastructure of
roastnet's own to operate or keep alive -- there is no "roastnet tracker"
anyone has to run. Every roastnet node announces itself under one fixed,
made-up info-hash (wan_discovery.SWARM_INFO_HASH); any other roastnet node
looking up that same info-hash finds it. This is exactly the mechanism a
"trackerless" torrent uses to find peers, borrowed for peer discovery
instead of file discovery -- confirmed working against the real public DHT
(dht.transmissionbt.com replied to a real `ping` sent from this project's
own dev sandbox during development).

Deliberately NOT a full DHT node: we only ever originate get_peers/
announce_peer/find_node queries (never route other people's lookups, never
maintain a persistent routing table/k-buckets). We do answer incoming
`ping`, both because it's trivial and because it makes us a slightly
better-behaved participant in someone else's routing table. Anything else
addressed to us is silently ignored -- acceptable for a lightweight client
that only needs its own two operations to work, not to help scale the
wider network.

Security note, same shape as lan_discovery's: nothing here is a trust
mechanism. A DHT-returned address is just a hint of "something might be
listening here" -- the actual roastnet "hello" handshake (wan_discovery)
and, after that, the QUIC handshake / signature verification / quota
checks in net.sync_with_peer are what anything found this way still has to
go through.
"""
from __future__ import annotations

import asyncio
import socket
import struct
from collections.abc import Callable

Addr = tuple[str, int]


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
    message) to carry roastnet's own unicast "hello" handshake -- the two
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
            lambda: _DhtProtocol(client), local_addr=("0.0.0.0", port),
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
        return await self._query(addr, "announce_peer", {
            b"id": self.own_id, b"info_hash": info_hash, b"port": 0,
            b"implied_port": 1, b"token": token,
        }, timeout=timeout)

    async def discover_and_announce_peers(
        self, info_hash: bytes, bootstrap_nodes: list[Addr], *,
        max_extra_hops: int = 16, timeout: float = 4.0,
    ) -> set[Addr]:
        """Shallow iterative lookup: query the bootstrap routers directly,
        follow up to `max_extra_hops` of the closer nodes they point back
        at, and announce ourselves (using each node's own token, as BEP 5
        requires) to every node that actually answered. Not a full
        Kademlia walk -- for a swarm the size roastnet's likely to have,
        the fixed bootstrap routers plus one hop out reach enough of the
        DHT to be useful, without the bookkeeping of a persistent routing
        table."""
        found: set[Addr] = set()
        queried: set[Addr] = set()
        extra_candidates: list[Addr] = []

        async def _visit(addr: Addr) -> None:
            queried.add(addr)
            resp = await self.get_peers(addr, info_hash, timeout=timeout)
            if resp is None:
                return
            for raw in resp.get(b"values") or []:
                found.update(decode_compact_peers(raw))
            nodes_blob = resp.get(b"nodes")
            if nodes_blob:
                for _node_id, node_addr in decode_compact_nodes(nodes_blob):
                    if node_addr not in queried and len(extra_candidates) < max_extra_hops:
                        extra_candidates.append(node_addr)
            token = resp.get(b"token")
            if token is not None:
                await self.announce_peer(addr, info_hash, token, timeout=timeout)

        for addr in bootstrap_nodes:
            if addr not in queried:
                await _visit(addr)
        for addr in extra_candidates:
            if addr not in queried:
                await _visit(addr)

        found.discard(("0.0.0.0", 0))
        return found
