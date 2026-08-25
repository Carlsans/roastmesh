"""dht.py: bencode/bdecode are pure and tested without any network. The one
network-touching test talks to the real public BitTorrent Mainline DHT
(the whole point of dht.py is to reuse that network) -- it self-skips if
there's no route to it, so the suite still passes fully offline.
"""
from __future__ import annotations

import asyncio
import socket

import pytest

from roastnet.dht import (
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


pytestmark_network = pytest.mark.skipif(
    not _real_dht_reachable(), reason="no route to the public BitTorrent DHT from here",
)


@pytestmark_network
async def test_ping_the_real_public_dht() -> None:
    client = await DhtClient.bind(port=0, own_id=b"q" * 20)
    try:
        ip = socket.gethostbyname("dht.transmissionbt.com")
        reply = await client.ping((ip, 6881), timeout=5.0)
        assert reply is not None
        assert len(reply[b"id"]) == 20
    finally:
        client.close()


@pytestmark_network
async def test_get_peers_against_the_real_public_dht_for_a_made_up_infohash() -> None:
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
