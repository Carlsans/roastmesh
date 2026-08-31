"""wan_discovery.py, decoupled from the real public DHT: `bootstrap_nodes`
points at a tiny fake DHT node this test runs itself, so the hello-exchange
and reciprocity logic is tested deterministically without depending on
internet reachability or the real swarm's timing.
"""
from __future__ import annotations

import asyncio
import json
import socket

import pytest

from roastmesh.dht import bdecode, bencode, encode_compact_addr
from roastmesh.dht import DhtClient, LookupStats, encode_compact_addr as _eca
from roastmesh.wan_discovery import (
    IP_VOTE_QUORUM,
    SWARM_INFO_HASH,
    default_state_path,
    diagnostics_payload,
    external_address,
    run_wan_discovery,
)


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
        node_cache_path=tmp_path / "nodes_a.json", allow_loopback=True,
    ))
    task_b = asyncio.create_task(run_wan_discovery(
        "bb" * 32, "ticket-b", on_b, port=port_b, lookup_interval_s=0.2, hello_resync_s=1.0,
        bootstrap_nodes=[("127.0.0.1", fake_port_for_b)],
        node_cache_path=tmp_path / "nodes_b.json", allow_loopback=True,
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
        node_cache_path=tmp_path / "nodes_a.json", allow_loopback=True,
    ))
    task_b = asyncio.create_task(run_wan_discovery(
        "bb" * 32, "ticket-b", on_b, port=port_b, lookup_interval_s=0.2, hello_resync_s=1.0,
        bootstrap_nodes=[("127.0.0.1", fake_port_for_b)],
        node_cache_path=tmp_path / "nodes_b.json", allow_loopback=True,
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


# --- external address & NAT verdict ----------------------------------------

class _Votes:
    """Just enough of a DhtClient for external_address()."""

    def __init__(self, votes):
        self.ip_votes = votes


def test_external_address_needs_a_quorum_before_it_will_commit() -> None:
    # One node's word is explicitly not enough: BEP 42 says a single node
    # cannot be trusted about our address, and acting on one would let any
    # peer talk us into a wrong (or attacker-chosen) node ID.
    addr, nat, votes = external_address(_Votes({("1.2.3.4", 5): 1}))
    assert (addr, nat) == (None, "unknown")
    assert votes == 1


def test_external_address_takes_the_majority_and_ignores_a_dissenter() -> None:
    addr, nat, votes = external_address(_Votes({
        ("209.227.189.65", 48973): IP_VOTE_QUORUM + 5,
        ("10.0.0.1", 48973): 1,
    }))
    assert addr == ("209.227.189.65", 48973)
    assert nat == "consistent"
    assert votes == IP_VOTE_QUORUM + 6


def test_differing_ports_across_responders_are_reported_as_symmetric_nat() -> None:
    """The signature that matters most for a user who "can't find anyone".

    One socket seen on two different ports means the NAT rewrites the source
    port per destination, so no other node can ever send us a first packet.
    That is unfixable from inside the DHT, and saying so is the difference
    between a useful diagnosis and another green-looking dead end.
    """
    _addr, nat, _votes = external_address(_Votes({
        ("209.227.189.65", 48973): 4,
        ("209.227.189.65", 51002): 3,
    }))
    assert nat == "symmetric"


# --- the diagnostics contract ----------------------------------------------

async def test_diagnostics_payload_is_the_shape_both_producers_promise() -> None:
    """`node doctor --json` and the live `wan-stats:` line must stay
    interchangeable -- the GUI panel reads whichever arrives first."""
    client = await DhtClient.bind(port=0, own_id=b"\x01" * 20, allow_loopback=True)
    try:
        stats = LookupStats()
        stats.rounds, stats.queried, stats.replied = 7, 40, 18
        stats.closest_bits, stats.announced = 140, 8
        stats.rejected_bep42 = 29
        stats.live_nodes = [(("1.2.3.4", 6881), b"\x02" * 20)]
        payload = diagnostics_payload(
            client, stats, info_hash=SWARM_INFO_HASH,
            external=("209.227.189.65", 48973), nat="consistent", votes=17,
            warm=True, readback=True, addrs={("5.6.7.8", 41890)},
        )
        assert set(payload) == {
            "external_ip", "external_port", "nat", "ip_votes", "node_id",
            "node_id_bep42", "routing_table", "warm", "lookup", "announce_set",
            "readback", "peers", "swarm_info_hash",
        }
        assert set(payload["lookup"]) == {
            "rounds", "queried", "replied", "closest_bits", "announced",
            "no_token", "peers_found", "rejected_martian",
            "rejected_impossible_proximity", "rejected_bep42",
        }
        assert payload["peers"] == ["5.6.7.8:41890"]
        assert payload["announce_set"] == [
            {"addr": "1.2.3.4:6881", "bits": 157, "bep42": False}]
        assert payload["swarm_info_hash"] == SWARM_INFO_HASH.hex()
        # Serialisable as-is: it is printed straight onto stdout as one line.
        assert json.loads(json.dumps(payload)) == payload
    finally:
        client.close()


# --- persistence ------------------------------------------------------------

def test_the_poisoned_node_cache_filename_is_gone() -> None:
    """`dht_nodes.json` was written from the k nodes closest to the swarm hash
    that answered -- exactly the set a sybil fleet controls -- so it re-seeded
    every restart back into the capture. It is abandoned, not migrated."""
    assert default_state_path().name == "dht_state.json"


async def test_state_is_written_to_the_new_file_and_the_old_one_is_never_read(tmp_path) -> None:
    poisoned = tmp_path / "dht_nodes.json"
    poisoned.write_text(json.dumps(
        [{"ip": "6.6.6.6", "port": 6881, "id": "00" * 20}]), encoding="utf-8")
    state = tmp_path / "dht_state.json"

    fake_dht, fake_port = await _start_fake_dht([("127.0.0.1", 41993)])
    task = asyncio.create_task(run_wan_discovery(
        "cc" * 32, "ticket-c", lambda *_a: asyncio.sleep(0), port=41994,
        lookup_interval_s=0.2, bootstrap_nodes=[("127.0.0.1", fake_port)],
        node_cache_path=state, allow_loopback=True,
    ))
    try:
        for _ in range(50):
            if state.exists():
                break
            await asyncio.sleep(0.1)
        assert state.exists(), "the new state file was never written"
        assert json.loads(poisoned.read_text(encoding="utf-8")) == [
            {"ip": "6.6.6.6", "port": 6881, "id": "00" * 20}
        ], "the old cache must be left untouched, not migrated"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        fake_dht.close()


async def test_a_cold_node_does_not_announce_itself(tmp_path) -> None:
    """A lookup that never gets near the swarm must not publish us.

    The fake DHT is one node whose ID is nowhere near the swarm hash, so the
    walk stops around 2^158 -- far outside ANNOUNCE_MAX_BITS. Announcing from
    there hands our address to nodes no stranger's lookup will ever ask, which
    is indistinguishable from not announcing at all except that it reads as
    success."""
    announced: list = []

    class _CountingFake(_FakeDhtNode):
        def datagram_received(self, data, addr):
            msg = bdecode(data)
            if msg.get(b"q") == b"announce_peer":
                announced.append(addr)
            super().datagram_received(data, addr)

    loop = asyncio.get_running_loop()
    transport, _proto = await loop.create_datagram_endpoint(
        lambda: _CountingFake([("127.0.0.1", 41995)]), local_addr=("127.0.0.1", 0))
    fake_port = transport.get_extra_info("sockname")[1]

    task = asyncio.create_task(run_wan_discovery(
        "dd" * 32, "ticket-d", lambda *_a: asyncio.sleep(0), port=41996,
        lookup_interval_s=0.2, bootstrap_nodes=[("127.0.0.1", fake_port)],
        node_cache_path=tmp_path / "state.json", allow_loopback=True,
    ))
    try:
        await asyncio.sleep(2.0)
        assert announced == [], f"announced from a cold routing table: {announced}"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        transport.close()
