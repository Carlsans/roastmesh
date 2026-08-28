"""dht.py: bencode/bdecode are pure and tested without any network. The one
network-touching test talks to the real public BitTorrent Mainline DHT
(the whole point of dht.py is to reuse that network) -- it self-skips if
there's no route to it, so the suite still passes fully offline.
"""
from __future__ import annotations

import asyncio
import hashlib
import socket

import pytest

from roastmesh.dht import (
    DhtClient,
    bdecode,
    bencode,
    decode_compact_nodes,
    decode_compact_peers,
    encode_compact_addr,
)


def test_bencode_bdecode_roundtrip_scalars() -> None:
    assert bdecode(bencode(42)) == 42
    assert bdecode(bencode(b"hello")) == b"hello"
    assert bdecode(bencode("hello")) == b"hello"


def test_bencode_bdecode_roundtrip_list_and_dict() -> None:
    value = {b"a": 1, b"b": [b"x", b"y", 3], b"c": {b"nested": b"yes"}}
    assert bdecode(bencode(value)) == value


def test_bencode_dict_keys_sorted() -> None:
    assert bencode({b"z": 1, b"a": 2}) == b"d1:ai2e1:zi1ee"


def test_bdecode_real_dht_ping_reply_sample() -> None:
    # A real reply this project received from dht.transmissionbt.com during
    # development (captured manually, not fabricated).
    sample = (b"d1:rd2:id20:yb\xb6X\x13\xb6\x97\xb1-\x1d:\xa5\xcd\x01\xe1\xda"
              b"$\x02\xc0\xe9e1:t2:aa1:v4:JB\x00\x001:y1:re")
    msg = bdecode(sample)
    assert msg[b"y"] == b"r"
    assert msg[b"t"] == b"aa"
    assert len(msg[b"r"][b"id"]) == 20


def test_compact_peer_and_node_roundtrip() -> None:
    addr = ("203.0.113.5", 6881)
    blob = encode_compact_addr(addr)
    assert decode_compact_peers(blob) == [addr]

    node_id = b"x" * 20
    nodes_blob = node_id + encode_compact_addr(addr)
    assert decode_compact_nodes(nodes_blob) == [(node_id, addr)]


def _real_dht_reachable() -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        sock.sendto(bencode({b"t": b"aa", b"y": b"q", b"q": b"ping", b"a": {b"id": b"x" * 20}}),
                    (socket.gethostbyname("dht.transmissionbt.com"), 6881))
        sock.recvfrom(2048)
        return True
    except OSError:
        return False
    finally:
        sock.close()


_reachable_cache: list[bool] = []


@pytest.fixture
def real_dht() -> None:
    """Skip unless the public DHT is actually reachable, probed lazily and at
    most once per session.

    A `skipif(...)` mark would evaluate its condition at *import* time, which
    is what this used to do: every `pytest` run of this repo -- including fully
    offline ones, and ones touching no networking at all -- fired a UDP packet
    at the public internet during collection and could stall for seconds before
    the first test ran."""
    if not _reachable_cache:
        _reachable_cache.append(_real_dht_reachable())
    if not _reachable_cache[0]:
        pytest.skip("no route to the public BitTorrent DHT from here")


async def test_ping_the_real_public_dht(real_dht) -> None:
    client = await DhtClient.bind(port=0, own_id=b"q" * 20)
    try:
        ip = socket.gethostbyname("dht.transmissionbt.com")
        reply = await client.ping((ip, 6881), timeout=5.0)
        assert reply is not None
        assert len(reply[b"id"]) == 20
    finally:
        client.close()


async def test_get_peers_against_the_real_public_dht_for_a_made_up_infohash(real_dht) -> None:
    # Nobody is announcing this exact info-hash, so this just proves the
    # request/response/token/nodes-parsing path works end to end against a
    # real, independent implementation of the protocol -- not that any
    # peers come back.
    client = await DhtClient.bind(port=0, own_id=b"r" * 20)
    try:
        ip = socket.gethostbyname("dht.transmissionbt.com")
        reply = await client.get_peers((ip, 6881), b"z" * 20, timeout=5.0)
        assert reply is not None
        # Real-world observed quirk: this well-known bootstrap router
        # replies to get_peers without a `token` (BEP 5 says one should
        # always be present) -- discover_and_announce_peers already
        # tolerates a missing token by simply skipping announce_peer for
        # that node, so this only asserts what's actually guaranteed.
        assert b"nodes" in reply or b"values" in reply
    finally:
        client.close()


@pytest.fixture
def live_dht_optin(real_dht) -> None:
    """Opt-in via `ROASTMESH_LIVE_DHT=1`, because this one is different in kind
    from the rest of the suite.

    It takes ~100 seconds, and it competes for the same per-IP rate limits as
    everything else on the machine -- including this suite's own GUI tests,
    which start real `node serve --wan-discovery` processes. Run right after
    those, the public DHT throttles us and the test fails for reasons that have
    nothing to do with the code. Left on by default it would be exactly the
    kind of test people learn to ignore, which is how the original bug survived
    in the first place. So: explicit, and documented as the way to *prove*
    internet discovery works.

        ROASTMESH_LIVE_DHT=1 pytest tests/test_dht.py -k announce_then_find -v
    """
    import os

    if os.environ.get("ROASTMESH_LIVE_DHT") != "1":
        pytest.skip("set ROASTMESH_LIVE_DHT=1 to run the live announce/lookup round trip")


async def test_announce_then_find_it_again_on_the_real_public_dht(live_dht_optin) -> None:
    """The decisive test, and the one whose absence let broken internet
    discovery ship: announce a random info-hash, then look it up from a
    *separate* socket and require the announced address to come back.

    This is what "internet discovery works" actually means, and it is checked
    against real third-party BEP 5 implementations rather than a fixture of our
    own design. It is deliberately strict about *which* address it accepts --
    public DHT nodes return sybil/spam `values` for arbitrary info-hashes
    (observed: three unrelated IPs all carrying the querying socket's own port),
    so merely asserting "some peers came back" passes while discovery is
    completely broken. A random info-hash keeps runs from colliding.
    """
    import os

    from roastmesh.dht import LookupStats
    from roastmesh.wan_discovery import DEFAULT_DHT_BOOTSTRAP, _resolve

    info_hash = hashlib.sha1(os.urandom(20)).digest()
    cache: dict = {}

    async def run(client: DhtClient, *, announce: bool) -> tuple[set, LookupStats]:
        seeds = list(dict.fromkeys([*(await _resolve(DEFAULT_DHT_BOOTSTRAP)), *cache]))
        stats = LookupStats()
        peers = await client.discover_and_announce_peers(
            info_hash, seeds, seed_ids=dict(cache), announce=announce, stats=stats,
        )
        cache.update(dict(stats.live_nodes))  # warm the next lookup, as serve() does
        return peers, stats

    announcer = await DhtClient.bind(port=0, own_id=hashlib.sha1(b"announcer").digest())
    seeker = await DhtClient.bind(port=0, own_id=hashlib.sha1(b"seeker").digest())
    try:
        _peers, first = await run(announcer, announce=True)
        if first.replied == 0:
            pytest.skip("public DHT did not answer at all right now")
        # Twice: the first pass mostly serves to warm the node cache, so the
        # second starts near the target instead of at a distant router.
        _peers, announced_stats = await run(announcer, announce=True)
        if announced_stats.announced == 0:
            pytest.skip("no storing node accepted an announce right now")

        found, seek_stats = await run(seeker, announce=False)
        my_ip = socket.gethostbyname(socket.gethostname())
        announced_port = announcer._transport.get_extra_info("sockname")[1]
        mine = [addr for addr in found if addr[1] == announced_port and addr[0] != my_ip]
        assert mine, (
            "announce was not retrievable by an independent lookup.\n"
            f"  announce: {announced_stats.summary()}\n"
            f"  lookup:   {seek_stats.summary()}\n"
            f"  returned: {sorted(found)} (entries not on port {announced_port} are DHT spam)"
        )
    finally:
        announcer.close()
        seeker.close()
