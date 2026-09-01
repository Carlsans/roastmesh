"""dht.py: bencode/bdecode are pure and tested without any network. The one
network-touching test talks to the real public BitTorrent Mainline DHT
(the whole point of dht.py is to reuse that network) -- it self-skips if
there's no route to it, so the suite still passes fully offline.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import socket
import time

import pytest

from roastmesh.dht import (
    ANNOUNCE_LIVE_COUNT,
    BEP42_CONTESTED_ZONE_THRESHOLD,
    BUCKET_MAX_COUNT,
    IMPOSSIBLE_PROXIMITY_THRESHOLD,
    K,
    NODE_EVICT_IDLE_S,
    NODE_EVICT_PINGED,
    PEER_EXPIRY_S,
    RATE_LIMIT_CAPACITY,
    SEARCH_NODES,
    TOKEN_SIZE,
    MAX_PEERS_REPLY,
    MAX_SEARCH_NODES_PER_PREFIX,
    MIN_USEFUL_RECV_BUFFER,
    SOURCE_BLOCK_S,
    SOURCE_BURST_WINDOW_S,
    Blacklist,
    DhtClient,
    Node,
    PeerStore,
    RoutingTable,
    Search,
    SourceLimiter,
    TokenBucket,
    TokenSecrets,
    bdecode,
    bencode,
    udp_socket,
    bep42_node_id,
    bep42_valid,
    crc32c,
    decode_compact_nodes,
    decode_compact_peers,
    distance,
    encode_compact_addr,
    is_martian,
    make_token,
    token_valid,
)


def _loopback_addr(client) -> tuple[str, int]:
    """Where to send so an in-process node actually receives it.

    `udp_socket` binds 0.0.0.0, so `sockname` reports 0.0.0.0 -- and sending
    to that happens to reach localhost on Linux while Windows rejects it
    outright with WinError 10049. Caught by the Windows CI run, which is the
    only thing that executes these paths on the target platform.
    """
    return ("127.0.0.1", client._transport.get_extra_info("sockname")[1])


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


# =============================================================================
# Phase A: pure functions -- BEP 42 node IDs, address hygiene, tokens.
# =============================================================================

# The spec's own published test vectors (BEP 42), not anything derived from
# our own implementation -- bold prefix is crc-derived, last byte is `rand`.
_BEP42_VECTORS = [
    ("124.31.75.21", 1, "5fbfbff10c5d6a4ec8a88e4c6ab4c28b95eee401"),
    ("21.75.31.124", 86, "5a3ce9c14e7a08645677bbd1cfe7d8f956d53256"),
    ("65.23.51.170", 22, "a5d43220bc8f112a3d426c84764f8c2a1150e616"),
    ("84.124.73.14", 65, "1b0321dd1bb1fe518101ceef99462b947a01ff41"),
    ("43.213.53.83", 90, "e56f6cbf5b7c4be0237986d5243b87aa6d51305a"),
]


@pytest.mark.parametrize("ip, rand, expected_hex", _BEP42_VECTORS)
def test_bep42_spec_vectors_verify(ip, rand, expected_hex) -> None:
    node_id = bytes.fromhex(expected_hex)
    assert node_id[19] == rand
    assert bep42_valid(node_id, ip) is True


@pytest.mark.parametrize("ip, rand, expected_hex", _BEP42_VECTORS)
def test_bep42_flipping_a_bit_in_the_first_21_bits_invalidates(ip, rand, expected_hex) -> None:
    node_id = bytearray(bytes.fromhex(expected_hex))
    node_id[0] ^= 0x01  # id[0] is entirely inside the checked 21 bits
    assert bep42_valid(bytes(node_id), ip) is False


@pytest.mark.parametrize("ip, rand, expected_hex", _BEP42_VECTORS)
def test_bep42_changing_only_the_random_middle_bytes_stays_valid(ip, rand, expected_hex) -> None:
    node_id = bytearray(bytes.fromhex(expected_hex))
    node_id[5] ^= 0xFF   # inside id[3..18] -- the spec leaves these unconstrained
    node_id[10] ^= 0xFF
    assert bep42_valid(bytes(node_id), ip) is True


def test_bep42_exempt_for_private_and_loopback_addresses() -> None:
    # None (exempt), not False -- the spec exempts non-routable addresses,
    # and a caller that conflates the two would wrongly reject every RFC1918
    # peer instead of just not requiring conformance from them.
    random_id = secrets.token_bytes(20)
    for ip in ("10.1.2.3", "172.20.0.5", "192.168.1.1", "127.0.0.1", "0.1.2.3"):
        assert bep42_valid(random_id, ip) is None


def test_bep42_node_id_round_trips_and_is_stable() -> None:
    seed = b"a roastmesh identity's ed25519 pubkey (36-byte stand-in)"
    ip = "198.51.100.42"
    node_id = bep42_node_id(ip, seed)
    assert bep42_valid(node_id, ip) is True
    assert bep42_node_id(ip, seed) == node_id          # stable across calls
    assert bep42_node_id(ip, seed + b"x") != node_id   # different identity -> different id


def test_is_martian_matches_dht_c_rules() -> None:
    assert is_martian(("203.0.113.5", 0)) is True          # port 0
    assert is_martian(("0.1.2.3", 6881)) is True            # 0.x
    assert is_martian(("127.0.0.1", 6881)) is True          # loopback
    assert is_martian(("224.0.0.1", 6881)) is True          # multicast
    assert is_martian(("255.255.255.255", 6881)) is True    # top 3 bits set (0xE0)
    assert is_martian(("203.0.113.5", 6881)) is False       # an ordinary routable address
    assert is_martian(("10.0.0.5", 6881)) is False           # RFC1918 -- BEP42-exempt, not martian


def test_blacklist_is_an_lru_of_bounded_size() -> None:
    bl = Blacklist(capacity=3)
    addrs = [(f"203.0.113.{i}", 6881) for i in range(4)]
    for a in addrs[:3]:
        bl.add(a)
    assert all(a in bl for a in addrs[:3])
    bl.add(addrs[3])  # evicts the oldest (addrs[0])
    assert addrs[0] not in bl
    assert all(a in bl for a in addrs[1:])
    assert len(bl) == 3


def test_make_token_is_deterministic_and_address_bound() -> None:
    secret = b"s3cr3t-0123456789"
    addr = ("203.0.113.5", 6881)
    t1 = make_token(addr, secret)
    assert make_token(addr, secret) == t1 and len(t1) == TOKEN_SIZE
    assert make_token(("203.0.113.6", 6881), secret) != t1
    assert make_token(addr, b"a-different-secret") != t1


def test_token_valid_accepts_current_and_previous_secret_only() -> None:
    addr = ("203.0.113.5", 6881)
    current, previous, stale = b"c" * 20, b"p" * 20, b"s" * 20
    tok = make_token(addr, previous)
    assert token_valid(tok, addr, current, previous) is True
    assert token_valid(tok, addr, current, None) is False   # no grace secret offered
    assert token_valid(tok, addr, current, stale) is False  # wrong previous secret


def test_token_secrets_rotate_on_schedule_and_grant_grace() -> None:
    owned = TokenSecrets(now=0.0)
    addr = ("203.0.113.5", 6881)
    tok = owned.make(addr, now=0.0)
    assert owned.valid(tok, addr, now=100.0) is True  # well before rotation

    rotated_at = owned._next_rotation
    # Past the rotation boundary, the *old* token is still honoured once --
    # this is the grace window; without it every token issued in the
    # seconds before a rotation would be refused on the announce_peer it
    # was meant to authorize.
    assert owned.valid(tok, addr, now=rotated_at + 1.0) is True
    fresh = owned.make(addr, now=rotated_at + 1.0)
    assert fresh != tok

    next_rotation = owned._next_rotation
    assert next_rotation > rotated_at
    # A second rotation retires the secret that granted the earlier grace.
    assert owned.valid(tok, addr, now=next_rotation + 1.0) is False


# =============================================================================
# Phase B: RoutingTable admission.
# =============================================================================

def test_routing_table_rejects_self() -> None:
    own_id = secrets.token_bytes(20)
    table = RoutingTable(own_id)
    assert table.new_node(own_id, ("203.0.113.1", 6881), confirm=2) is None
    assert len(table) == 0


def test_routing_table_dedupe_by_id_updates_address() -> None:
    own_id = secrets.token_bytes(20)
    table = RoutingTable(own_id)
    node_id = secrets.token_bytes(20)
    first = table.new_node(node_id, ("203.0.113.1", 6881), confirm=2, now=1000.0)
    assert first is not None and first.addr == ("203.0.113.1", 6881)

    moved = table.new_node(node_id, ("203.0.113.2", 6882), confirm=2, now=1001.0)
    assert moved is first                        # same slot, not a duplicate
    assert moved.addr == ("203.0.113.2", 6882)    # address updated in place
    assert len(table) == 1


def test_routing_table_rejects_martian_and_blacklisted_addresses() -> None:
    own_id = secrets.token_bytes(20)
    table = RoutingTable(own_id, allow_loopback=False)  # the real dht.c rule, loopback included
    for addr in [("127.0.0.1", 6881), ("0.1.2.3", 6881), ("203.0.113.9", 0), ("224.0.0.1", 6881)]:
        assert table.new_node(secrets.token_bytes(20), addr, confirm=2) is None
    assert len(table) == 0

    good_addr = ("203.0.113.9", 6881)
    assert table.new_node(secrets.token_bytes(20), good_addr, confirm=2) is not None
    table.blacklist.add(good_addr)
    assert table.new_node(secrets.token_bytes(20), good_addr, confirm=2) is None  # blacklisted now


def test_routing_table_full_bucket_pings_a_dubious_node_instead_of_evicting() -> None:
    own_id = b"\x00" * 20
    table = RoutingTable(own_id, allow_loopback=True)
    # Fill the (only, own) bucket to capacity. The first one is planted with
    # an old reply_time so it's gone dubious (node_good() false) by the time
    # we probe the table again; the rest stay fresh/good.
    ids = [secrets.token_bytes(20) for _ in range(BUCKET_MAX_COUNT)]
    for i, node_id in enumerate(ids):
        confirm_time = 1000.0 if i == 0 else 9000.0
        table.new_node(node_id, (f"203.0.113.{i % 254 + 1}", 20000 + i), confirm=2, now=confirm_time)
    assert len(table) == BUCKET_MAX_COUNT

    pinged: list[Node] = []
    table._on_dubious = pinged.append  # phase D's real UDP ping hooks in here
    result = table.new_node(secrets.token_bytes(20), ("203.0.113.2", 30000), confirm=0, now=9000.0)

    assert result is None                     # bucket stayed full -- newcomer not admitted
    assert len(table) == BUCKET_MAX_COUNT     # nobody evicted
    assert [n.id for n in pinged] == [ids[0]]  # exactly the stale one was chosen
    assert pinged[0].pinged == 1


def test_routing_table_splits_its_own_bucket_when_full_of_good_nodes() -> None:
    own_id = b"\x00" * 20  # top bit 0
    table = RoutingTable(own_id, allow_loopback=True)
    # Every planted ID has its top bit set to 1 -- the opposite of our own --
    # so the routing table's first-ever split (always at the MSB) puts all
    # eight on the far side, deterministically freeing our own bucket for
    # the newcomer rather than depending on random luck to avoid re-filling it.
    for i in range(BUCKET_MAX_COUNT):
        node_id = bytes([0x80]) + secrets.token_bytes(19)
        table.new_node(node_id, (f"203.0.113.{i % 254 + 1}", 20000 + i), confirm=2, now=1000.0)
    assert len(table._buckets) == 1
    assert len(table) == BUCKET_MAX_COUNT

    newcomer = bytes([0x00]) + secrets.token_bytes(19)  # top bit 0 -- our side
    # An address none of the planted nodes used -- one entry per IP is now a
    # rule of the table, not an accident of the fixture.
    result = table.new_node(newcomer, ("203.0.113.200", 30000), confirm=2, now=1000.0)

    assert result is not None and result.id == newcomer
    assert len(table._buckets) == 2
    assert len(table) == BUCKET_MAX_COUNT + 1
    # Halved on the split, per dht.c: the space around our own ID is held to
    # Kademlia's replication factor, not left at the coarse bucket's width.
    assert [b.max_count for b in table._buckets] == [BUCKET_MAX_COUNT // 2] * 2


# =============================================================================
# Phase B: Search admission -- the filters that are the actual fix.
# =============================================================================

def test_search_keeps_at_most_search_nodes_slots_sorted_by_distance() -> None:
    target = secrets.token_bytes(20)
    search = Search(target)
    for i in range(SEARCH_NODES + 5):
        # A different /24 each: MAX_SEARCH_NODES_PER_PREFIX deliberately stops
        # one network filling a search's frontier, which is not what this test
        # is about.
        search.insert(secrets.token_bytes(20), (f"198.51.{i}.1", 20000 + i), replied=False)
    assert len(search._nodes) == SEARCH_NODES
    dists = [search._dist(n.id) for n in search._nodes]
    assert dists == sorted(dists)


def test_search_rejects_martian_addresses_in_strict_mode() -> None:
    """`allow_loopback` (default True -- see RoutingTable._martian) exists
    only to let this repo's in-process test harnesses exercise real UDP
    traffic over loopback; the underlying rule is still there and is what
    a production node (which never opts into allow_loopback) actually
    runs. `allow_loopback=False` here is what exercises it directly."""
    target = secrets.token_bytes(20)
    search = Search(target, allow_loopback=False)
    assert search.insert(secrets.token_bytes(20), ("127.0.0.1", 6881), replied=True) is None
    assert search.rejected_martian == 1


def test_search_rejects_impossible_proximity_and_blacklists() -> None:
    target = secrets.token_bytes(20)
    search = Search(target)
    forged_id = target[:15] + secrets.token_bytes(5)  # shares 15 bytes -- the live fleet's trick
    addr = ("203.0.113.1", 6881)
    assert distance(forged_id, target) < IMPOSSIBLE_PROXIMITY_THRESHOLD

    assert search.insert(forged_id, addr, replied=True) is None
    assert search.rejected_impossible_proximity == 1
    assert addr in search.blacklist
    # Blacklisted now -- even an otherwise-unremarkable claim from that
    # address is refused without re-evaluating it.
    assert search.insert(secrets.token_bytes(20), addr, replied=True) is None


def test_search_requires_bep42_inside_the_contested_zone_but_not_outside() -> None:
    target = secrets.token_bytes(20)
    search = Search(target)

    # Inside the zone (<2^145): only the top 21 bits are forced to agree
    # with `target` (via a single flipped bit at that boundary), so this is
    # still effectively a random, non-conforming BEP 42 claim.
    near_id = bytearray(target)
    near_id[3] ^= 0x04  # flips bit 29 (MSB-numbered) -> XOR distance == 2**130
    near_id = bytes(near_id)
    near_ip = "203.0.113.5"
    assert IMPOSSIBLE_PROXIMITY_THRESHOLD <= distance(near_id, target) < BEP42_CONTESTED_ZONE_THRESHOLD
    assert bep42_valid(near_id, near_ip) is False
    assert search.insert(near_id, (near_ip, 6881), replied=True) is None
    assert search.rejected_bep42 == 1

    # Outside the zone: the *same kind* of non-conforming claim is only a
    # ranking preference, not a hard requirement -- roughly half the honest
    # network predates BEP 42, and this is what keeps it usable for routing.
    far_id = secrets.token_bytes(20)
    far_ip = "198.51.100.7"
    assert distance(far_id, target) >= BEP42_CONTESTED_ZONE_THRESHOLD
    node = search.insert(far_id, (far_ip, 6881), replied=True)
    assert node is not None


# =============================================================================
# Phase C: serving -- find_node/get_peers/announce_peer, tokens, rate limits.
# =============================================================================

async def test_ip_votes_are_parsed_from_the_top_level_ip_field_of_replies() -> None:
    """Phase D's external-address/NAT detection reads `ip_votes`; nothing
    here acts on it yet, but the parsing itself -- and the "one vote per
    distinct responder" dedupe -- has to work now."""
    server = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        server_addr = _loopback_addr(server)
        client_port = client._transport.get_extra_info("sockname")[1]

        assert client.ip_votes == {}
        await client.ping(server_addr, timeout=2.0)
        assert client.ip_votes == {("127.0.0.1", client_port): 1}

        # A second reply from the *same* responder must not inflate the count.
        await client.ping(server_addr, timeout=2.0)
        assert client.ip_votes == {("127.0.0.1", client_port): 1}
    finally:
        server.close()
        client.close()


async def test_serves_ping_find_node_get_peers_announce_peer() -> None:
    server = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        server_addr = _loopback_addr(server)

        pong = await client.ping(server_addr, timeout=2.0)
        assert pong is not None and pong[b"id"] == server.own_id
        # `ping()` hands back the "r" sub-dict, so the echoed address is not
        # visible here at all -- it lives one level up. Asserting `b"ip" in
        # pong` is what let the field sit in the wrong place unnoticed; the
        # wire format is pinned by
        # test_the_echoed_ip_field_is_top_level_on_the_wire instead, and the
        # round trip by the ip_votes test below.
        assert b"ip" not in pong

        target = secrets.token_bytes(20)
        fn = await client.find_node(server_addr, target, timeout=2.0)
        assert fn is not None and b"nodes" in fn

        info_hash = secrets.token_bytes(20)
        gp = await client.get_peers(server_addr, info_hash, timeout=2.0)
        assert gp is not None
        token = gp[b"token"]
        assert len(token) == TOKEN_SIZE
        assert b"values" not in gp  # nothing announced yet -- closest nodes instead
        assert b"nodes" in gp

        ann = await client.announce_peer(server_addr, info_hash, token, timeout=2.0)
        assert ann is not None

        gp2 = await client.get_peers(server_addr, info_hash, timeout=2.0)
        assert gp2 is not None and b"values" in gp2
        client_port = client._transport.get_extra_info("sockname")[1]
        peers = decode_compact_peers(b"".join(gp2[b"values"]))
        assert ("127.0.0.1", client_port) in peers
    finally:
        server.close()
        client.close()


async def test_announce_peer_with_a_bad_token_is_refused() -> None:
    server = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        server_addr = _loopback_addr(server)
        info_hash = secrets.token_bytes(20)
        result = await client.announce_peer(server_addr, info_hash, b"not-a-real-tok", timeout=2.0)
        assert result is None  # BEP 5 error reply -- treated the same as no answer

        gp = await client.get_peers(server_addr, info_hash, timeout=2.0)
        assert gp is not None and b"values" not in gp  # nothing was actually stored
    finally:
        server.close()
        client.close()


def test_token_bucket_caps_a_burst_then_refuses() -> None:
    bucket = TokenBucket(capacity=5, refill_per_s=1.0, now=0.0)
    for _ in range(5):
        assert bucket.take(now=0.0) is True
    assert bucket.take(now=0.0) is False  # exhausted, no time has passed
    assert RATE_LIMIT_CAPACITY == 400     # dht.c's actual production figure, unmodified


def test_token_bucket_refills_over_time() -> None:
    bucket = TokenBucket(capacity=5, refill_per_s=2.0, now=0.0)
    for _ in range(5):
        assert bucket.take(now=0.0) is True
    assert bucket.take(now=0.4) is False  # int(2.0 * 0.4) == 0 tokens -- too soon
    assert bucket.take(now=1.4) is True   # int(2.0 * 1.0) == 2 tokens accrued since then


def test_peer_store_expires_announced_peers_after_32_minutes() -> None:
    store = PeerStore()
    info_hash = secrets.token_bytes(20)
    addr = ("203.0.113.9", 6881)
    store.store(info_hash, addr, now=0.0)
    assert store.get(info_hash, now=0.0) == [addr]
    assert store.get(info_hash, now=PEER_EXPIRY_S - 1.0) == [addr]  # not expired yet
    assert store.get(info_hash, now=PEER_EXPIRY_S + 1.0) == []      # expired


async def test_rate_limiter_caps_incoming_requests() -> None:
    """Direct manipulation rather than 400 real round trips (slow, and racy
    under load) -- proves the same code path `_handle_message` actually
    calls refuses a request once the bucket is empty."""
    server = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        server_addr = _loopback_addr(server)
        assert await client.ping(server_addr, timeout=2.0) is not None

        server._rate_limiter._tokens = 0
        server._rate_limiter._last = time.monotonic()  # refill needs real elapsed time
        assert await client.ping(server_addr, timeout=0.5) is None  # dropped, not refused

        server._rate_limiter._tokens = 3  # restored -- e.g. a slice of real elapsed time later
        assert await client.ping(server_addr, timeout=2.0) is not None
    finally:
        server.close()
        client.close()


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
        pytest.skip("set ROASTMESH_LIVE_DHT=1 to run the live public-DHT tests")


async def test_ping_the_real_public_dht(live_dht_optin) -> None:
    client = await DhtClient.bind(port=0, own_id=b"q" * 20)
    try:
        ip = socket.gethostbyname("dht.transmissionbt.com")
        reply = await client.ping((ip, 6881), timeout=5.0)
        assert reply is not None
        assert len(reply[b"id"]) == 20
    finally:
        client.close()


async def test_get_peers_against_the_real_public_dht_for_a_made_up_infohash(live_dht_optin) -> None:
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


async def test_the_echoed_ip_field_is_top_level_on_the_wire() -> None:
    """BEP 42's `ip` field must sit beside "t"/"y"/"r", not inside "r".

    Pinned at the byte level, from a plain socket, because the mistake is
    self-concealing: when this server wrote the field into "r" its own reader
    looked there too, so both ends agreed and every test passed -- while the
    field was invisible to every real client, and every real client's `ip` was
    invisible to us. That silently disables the only way a node behind NAT can
    learn its own external address.
    """
    server = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    try:
        sock.bind(("127.0.0.1", 0))
        server_addr = _loopback_addr(server)
        query = bencode({b"t": b"aa", b"y": b"q", b"q": b"ping",
                         b"a": {b"id": secrets.token_bytes(20)}})
        await asyncio.get_running_loop().run_in_executor(None, sock.sendto, query, server_addr)
        raw, _ = await asyncio.get_running_loop().run_in_executor(None, sock.recvfrom, 4096)
        reply = bdecode(raw)

        assert b"ip" in reply, "the ip field must be in the top-level dictionary"
        assert b"ip" not in reply[b"r"], "the ip field must NOT be nested inside r"
        assert reply[b"ip"] == encode_compact_addr(sock.getsockname())
    finally:
        sock.close()
        server.close()


async def test_announcing_a_forwarded_port_publishes_that_port_not_the_source_port() -> None:
    """The whole point of `--public-port`, pinned at the wire level.

    Behind a port forward the reachable port and the outbound source port are
    different numbers -- measured on a Raspberry Pi with PIA port forwarding,
    inbound to the forwarded port worked perfectly while packets leaving that
    same socket got a fresh random source port every time. BEP 5's
    `implied_port` says "use the port you saw", which there publishes an
    address nobody can reach. Sending `implied_port=0` with an explicit port
    is the only thing that works, and this is what proves we do it.
    """
    server = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        server_addr = _loopback_addr(server)
        source_port = client._transport.get_extra_info("sockname")[1]
        forwarded = 26513
        assert forwarded != source_port

        info_hash = secrets.token_bytes(20)
        token = (await client.get_peers(server_addr, info_hash, timeout=2.0))[b"token"]
        assert await client.announce_peer(server_addr, info_hash, token, timeout=2.0,
                                          public_port=forwarded) is not None

        stored = decode_compact_peers(b"".join(
            (await client.get_peers(server_addr, info_hash, timeout=2.0))[b"values"]))
        assert ("127.0.0.1", forwarded) in stored, stored
        assert ("127.0.0.1", source_port) not in stored, (
            "the source port was published anyway -- implied_port was not switched off")
    finally:
        server.close()
        client.close()


async def test_without_a_forwarded_port_the_source_port_is_still_what_gets_published() -> None:
    """The default must not change: an ordinary NAT rewrites the source port,
    so `implied_port` -- "use the port you saw" -- is the only correct answer
    there, and it is what almost every node should keep sending."""
    server = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        server_addr = _loopback_addr(server)
        source_port = client._transport.get_extra_info("sockname")[1]
        info_hash = secrets.token_bytes(20)
        token = (await client.get_peers(server_addr, info_hash, timeout=2.0))[b"token"]
        await client.announce_peer(server_addr, info_hash, token, timeout=2.0)

        stored = decode_compact_peers(b"".join(
            (await client.get_peers(server_addr, info_hash, timeout=2.0))[b"values"]))
        assert ("127.0.0.1", source_port) in stored, stored
    finally:
        server.close()
        client.close()


# =============================================================================
# Hardening taken from libtorrent/Transmission after the first release worked.
# =============================================================================

def test_one_flooding_source_does_not_deny_service_to_everyone_else() -> None:
    """What the global token bucket cannot express.

    A single bucket answers "are we being asked too much" and never "by whom",
    so one noisy address could spend the whole 400-token budget and every other
    node on the network was refused as a result -- a stranger deciding who we
    talk to. libtorrent keeps both budgets for this reason.
    """
    limiter = SourceLimiter()
    flooder, bystander = "198.51.100.5", "203.0.113.9"

    blocked_at = None
    for i in range(200):
        if not limiter.allow(flooder, now=1.0):
            blocked_at = i
            break
    assert blocked_at is not None, "a flooding source was never blocked"
    assert limiter.allow(bystander, now=1.0), "an innocent address was refused too"


def test_a_blocked_source_is_forgiven_after_the_timeout() -> None:
    limiter = SourceLimiter()
    ip = "198.51.100.5"
    for _ in range(200):
        limiter.allow(ip, now=1.0)
    assert not limiter.allow(ip, now=1.0)
    assert limiter.allow(ip, now=1.0 + SOURCE_BLOCK_S + 1.0)


def test_slow_traffic_is_never_mistaken_for_a_flood() -> None:
    """Spread the same number of messages over a long enough period and it is
    just an ordinary busy peer."""
    limiter = SourceLimiter()
    ip = "198.51.100.5"
    now = 0.0
    for _ in range(100):
        assert limiter.allow(ip, now=now)
        now += SOURCE_BURST_WINDOW_S + 1.0


def test_one_public_address_gets_one_routing_table_slot() -> None:
    """libtorrent's dht_restrict_routing_ips. Ports are free, addresses are
    not -- otherwise one host binds sixteen sockets and owns a bucket."""
    table = RoutingTable(b"\x00" * 20, allow_loopback=True)
    first = table.new_node(secrets.token_bytes(20), ("203.0.113.7", 6881), confirm=2, now=1.0)
    second = table.new_node(secrets.token_bytes(20), ("203.0.113.7", 6882), confirm=2, now=1.0)
    assert first is not None
    assert second is None, "a second identity from one public address was admitted"


def test_several_nodes_may_share_a_private_address() -> None:
    """The exemption matters as much as the rule: a household behind one NAT,
    or an in-process test swarm on loopback, really is several nodes at one
    address, and only on the public internet is that evidence of anything."""
    table = RoutingTable(b"\x00" * 20, allow_loopback=True)
    kept = [table.new_node(secrets.token_bytes(20), ("127.0.0.1", 6881 + i), confirm=2, now=1.0)
            for i in range(4)]
    assert all(n is not None for n in kept)


def test_one_network_cannot_fill_a_search_frontier() -> None:
    """libtorrent's dht_restrict_search_ips: nodes this close together in CIDR
    terms are not independent, so a rented /24 must not be able to surround a
    target even if every ID in it verifies."""
    target = secrets.token_bytes(20)
    search = Search(target)
    admitted = [search.insert(secrets.token_bytes(20), (f"203.0.113.{i}", 6881), replied=False)
                for i in range(1, 8)]
    assert sum(1 for a in admitted if a is not None) == MAX_SEARCH_NODES_PER_PREFIX
    assert search.rejected_prefix == 7 - MAX_SEARCH_NODES_PER_PREFIX


async def test_a_get_peers_reply_is_capped() -> None:
    """dht_max_peers_reply. A reply is bigger than the query that provokes it,
    so an unbounded one is a gift to anyone spoofing a source address."""
    server = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        info_hash = secrets.token_bytes(20)
        for i in range(MAX_PEERS_REPLY + 50):
            server._peer_store.store(info_hash, (f"198.51.{i // 254}.{i % 254 + 1}", 6881))
        reply = await client.get_peers(_loopback_addr(server), info_hash, timeout=2.0)
        assert reply is not None and b"values" in reply
        assert len(reply[b"values"]) == MAX_PEERS_REPLY
    finally:
        server.close()
        client.close()


def test_the_udp_socket_asks_for_a_large_receive_buffer() -> None:
    """Transmission sets these explicitly because the default is small enough
    that a burst of DHT traffic is simply dropped -- which from inside looks
    exactly like an unreliable network, with no error anywhere to say so."""
    sock = udp_socket(0)
    try:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) >= MIN_USEFUL_RECV_BUFFER
    finally:
        sock.close()


async def test_a_read_only_node_says_so_and_is_not_routed_to() -> None:
    """BEP 43, both directions.

    A node behind a symmetric NAT can query perfectly well and simply cannot be
    queried. Saying so keeps it out of routing tables where it would answer
    nobody -- and honouring other nodes' flags keeps ours free of the same dead
    weight. It is politeness, not a fix: it does not make anyone reachable.
    """
    server = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
    try:
        server_addr = _loopback_addr(server)

        client.read_only = True
        reply = await client.ping(server_addr, timeout=2.0)
        assert reply is not None, "a read-only node must still get answers"
        assert server.routing_table.find(client.own_id) is None, (
            "a node that told us it is unreachable took a routing-table slot anyway")

        client.read_only = False
        assert await client.ping(server_addr, timeout=2.0) is not None
        assert server.routing_table.find(client.own_id) is not None
    finally:
        server.close()
        client.close()


async def test_the_read_only_flag_is_top_level_on_the_wire() -> None:
    """Beside "y", not inside "a" -- pinned at the byte level for the same
    reason BEP 42's `ip` field is: a flag in the wrong place is invisible to
    every other implementation while looking perfectly correct from here."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    try:
        sock.bind(("127.0.0.1", 0))
        listener = _loopback_addr_of_raw(sock)
        client = await DhtClient.bind(port=0, own_id=secrets.token_bytes(20), allow_loopback=True)
        client.read_only = True
        try:
            asyncio.get_running_loop().create_task(client.ping(listener, timeout=1.0))
            raw, _ = await asyncio.get_running_loop().run_in_executor(None, sock.recvfrom, 4096)
        finally:
            client.close()
        query = bdecode(raw)
        assert query[b"ro"] == 1, "the read-only flag must be in the top-level dictionary"
        assert b"ro" not in query[b"a"]
    finally:
        sock.close()


def _loopback_addr_of_raw(sock) -> tuple[str, int]:
    return ("127.0.0.1", sock.getsockname()[1])
