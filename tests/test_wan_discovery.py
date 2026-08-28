"""wan_discovery.py, decoupled from the real public DHT: `bootstrap_nodes`
points at a tiny fake DHT node this test runs itself, so the hello-exchange
and reciprocity logic is tested deterministically without depending on
internet reachability or the real swarm's timing.
"""
from __future__ import annotations

import asyncio
import socket

import pytest

from roastmesh.dht import bdecode, bencode, encode_compact_addr
from roastmesh.wan_discovery import run_wan_discovery


class _FakeDhtNode(asyncio.DatagramProtocol):
    """Answers get_peers/announce_peer just enough to hand back a set of
    peer addresses supplied by the test -- stands in for the real public
    DHT so tests are fast and hermetic."""

    def __init__(self, peers_to_report: list[tuple[str, int]]) -> None:
        self._peers = peers_to_report
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        msg = bdecode(data)
        t = msg[b"t"]
        q = msg[b"q"]
        if q == b"get_peers":
            values = [encode_compact_addr(p) for p in self._peers]
            reply = {b"t": t, b"y": b"r", b"r": {b"id": b"f" * 20, b"token": b"tok", b"values": values}}
        elif q == b"announce_peer":
            reply = {b"t": t, b"y": b"r", b"r": {b"id": b"f" * 20}}
        else:
            return
        self.transport.sendto(bencode(reply), addr)


async def _start_fake_dht(peers_to_report: list[tuple[str, int]]) -> tuple[asyncio.DatagramTransport, int]:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _FakeDhtNode(peers_to_report), local_addr=("127.0.0.1", 0),
    )
    port = transport.get_extra_info("sockname")[1]
    return transport, port


async def test_two_nodes_find_each_other_via_a_fake_dht_and_exchange_hellos(tmp_path) -> None:
    # Node B's fake DHT tells it about node A's real WAN-discovery port,
    # and vice versa -- exactly what the real public DHT would do once both
    # sides have announced, just without waiting on it.
    port_a, port_b = 41991, 41992

    fake_dht_for_a, fake_port_for_a = await _start_fake_dht([("127.0.0.1", port_b)])
    fake_dht_for_b, fake_port_for_b = await _start_fake_dht([("127.0.0.1", port_a)])

    discovered_by_a: list[tuple[str, str]] = []
    discovered_by_b: list[tuple[str, str]] = []

    async def on_a(pubkey: str, ticket: str) -> None:
        discovered_by_a.append((pubkey, ticket))

    async def on_b(pubkey: str, ticket: str) -> None:
        discovered_by_b.append((pubkey, ticket))

    task_a = asyncio.create_task(run_wan_discovery(
        "aa" * 32, "ticket-a", on_a, port=port_a, lookup_interval_s=0.2, hello_resync_s=1.0,
        bootstrap_nodes=[("127.0.0.1", fake_port_for_a)],
        node_cache_path=tmp_path / "nodes_a.json",
    ))
    task_b = asyncio.create_task(run_wan_discovery(
        "bb" * 32, "ticket-b", on_b, port=port_b, lookup_interval_s=0.2, hello_resync_s=1.0,
        bootstrap_nodes=[("127.0.0.1", fake_port_for_b)],
        node_cache_path=tmp_path / "nodes_b.json",
    ))
    try:
        # 20s, not the original 5s: this window flaked twice, only ever in a
        # full-suite run alongside the real-iroh and GUI-subprocess tests --
        # exactly when the event loop is most starved. The hello retry
        # schedule alone spans 6s, so 5s never had headroom. Exits as soon as
        # both sides have discovered each other, so passing runs are no slower.
        for _ in range(200):
            if discovered_by_a and discovered_by_b:
                break
            await asyncio.sleep(0.1)
        assert discovered_by_a == [("bb" * 32, "ticket-b")]
        assert discovered_by_b == [("aa" * 32, "ticket-a")]
    finally:
        task_a.cancel()
        task_b.cancel()
        for t in (task_a, task_b):
            try:
                await t
            except asyncio.CancelledError:
                pass
        fake_dht_for_a.close()
        fake_dht_for_b.close()


async def test_reciprocal_hello_reaches_a_node_the_fake_dht_never_told_about_the_sender(tmp_path) -> None:
    # Node B's fake DHT knows about A, but A's fake DHT reports nobody --
    # A still finds out about B once B's hello arrives, because B's hello
    # handler immediately hellos back to the sender's actual address.
    port_a, port_b = 41993, 41994

    fake_dht_for_a, fake_port_for_a = await _start_fake_dht([])
    fake_dht_for_b, fake_port_for_b = await _start_fake_dht([("127.0.0.1", port_a)])

    discovered_by_a: list[tuple[str, str]] = []

    async def on_a(pubkey: str, ticket: str) -> None:
        discovered_by_a.append((pubkey, ticket))

    async def on_b(pubkey: str, ticket: str) -> None:
        pass

    task_a = asyncio.create_task(run_wan_discovery(
        "aa" * 32, "ticket-a", on_a, port=port_a, lookup_interval_s=0.2, hello_resync_s=1.0,
        bootstrap_nodes=[("127.0.0.1", fake_port_for_a)],
        node_cache_path=tmp_path / "nodes_a.json",
    ))
    task_b = asyncio.create_task(run_wan_discovery(
        "bb" * 32, "ticket-b", on_b, port=port_b, lookup_interval_s=0.2, hello_resync_s=1.0,
        bootstrap_nodes=[("127.0.0.1", fake_port_for_b)],
        node_cache_path=tmp_path / "nodes_b.json",
    ))
    try:
        # 20s, not the original 5s: this window flaked twice, only ever in a
        # full-suite run alongside the real-iroh and GUI-subprocess tests --
        # exactly when the event loop is most starved. The hello retry
        # schedule alone spans 6s, so 5s never had headroom. Exits as soon as
        # both sides have discovered each other, so passing runs are no slower.
        for _ in range(200):
            if discovered_by_a:
                break
            await asyncio.sleep(0.1)
        assert discovered_by_a == [("bb" * 32, "ticket-b")]
    finally:
        task_a.cancel()
        task_b.cancel()
        for t in (task_a, task_b):
            try:
                await t
            except asyncio.CancelledError:
                pass
        fake_dht_for_a.close()
        fake_dht_for_b.close()


async def test_resolve_returns_only_ipv4_addresses() -> None:
    """The DHT stack is IPv4-only -- BEP 5 compact addresses are 4 bytes and
    DhtClient binds an IPv4 socket -- so resolution must ask for A records
    explicitly.

    Without `family=AF_INET`, `getaddrinfo` returns AAAA first on any
    IPv6-preferring host and result [0] is then an IPv6 address handed to an
    IPv4 socket: every query vanishes and the node never reaches the DHT at
    all. Reproduced on a real dual-stack machine, where roastmesh reported the
    DHT unreachable while a raw IPv4 probe to the same router answered
    instantly.
    """
    import ipaddress

    from roastmesh.wan_discovery import _resolve

    resolved = await _resolve([("dht.transmissionbt.com", 6881),
                               ("dht.libtorrent.org", 25401)])
    if not resolved:
        pytest.skip("no DNS available to resolve the bootstrap routers")
    for ip, _port in resolved:
        assert isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address), (
            f"{ip} is not IPv4 -- an IPv4 socket cannot reach it"
        )
