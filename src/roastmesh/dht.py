"""A BitTorrent Mainline DHT (BEP 5) client -- used to `get_peers`/
`announce_peer`/serve against the real, already-running, public BitTorrent
DHT (entered via the same well-known routers real BitTorrent clients use --
though of the traditionally-cited ones only dht.transmissionbt.com and
dht.libtorrent.org still answer; see wan_discovery.DEFAULT_DHT_BOOTSTRAP).

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

**This module used to be sybil-blind, and it was measured being eaten
alive.** A lookup for the fixed swarm info-hash converged to nodes at
2^36-2^39 from the target (impossible for an honest node -- the real
closest in an ~8.4M-node network is ~2^136) and announced to them; they
were a sybil fleet answering *every* `get_peers` with node IDs forged to
share the first 15 bytes of whatever info-hash was queried, plus fabricated
`values`. The routing table (`RoutingTable`), the per-lookup candidate list
(`Search`), and BEP 42 node-ID verification (`bep42_valid`) below exist
specifically to reject that: a candidate has to pass address hygiene
(`is_martian`), a proximity sanity check, and -- inside the zone close
enough to a target that it matters -- BEP 42 conformance, before it can
enter either structure or contribute a `values` entry. See the plan doc
(`proud-leaping-stearns.md`) for the live measurements this was built
against, and Juliusz Chroboczek's `dht.c` (bundled by Transmission) for the
routing-table/search mechanics this ports.

Also, unlike before, we now answer `find_node`/`get_peers`/`announce_peer`
(not just `ping`), rate-limited the same way `dht.c` rate-limits itself --
being a better-behaved participant in the wider DHT is also what makes
*our own* announces findable by other honest nodes.

Security note, same shape as lan_discovery's: nothing here is a trust
mechanism. A DHT-returned address is just a hint of "something might be
listening here" -- the actual roastmesh "hello" handshake (wan_discovery)
and, after that, the QUIC handshake / signature verification / quota
checks in net.sync_with_peer are what anything found this way still has to
go through.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import socket
import struct
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

Addr = tuple[str, int]

# Kademlia parameters. `K` is the replication factor: a value announced for a
# target is stored on the K nodes whose IDs are XOR-closest to it, so a lookup
# that does not reach those K nodes finds nothing, no matter how many other
# nodes it asks. This is BEP 5 / the Kademlia paper's value, and also `dht.c`'s
# SEARCH_NODES... no -- `dht.c` keeps 14 *candidates* per search (SEARCH_NODES,
# below) but still only ever announces to the first 8 that answer.
K = 8

# --- `dht.c`'s search mechanics, ported verbatim where they're just numbers ---
# 14 candidate slots per search, more than K so a search has somewhere to keep
# the runners-up while it's still converging on the true 8 closest.
SEARCH_NODES = 14
# How many get_peers queries a search keeps in flight at once.
DHT_INFLIGHT_QUERIES = 4
# Don't re-query the same not-yet-replied node more often than this.
DHT_SEARCH_RETRANSMIT_S = 10.0
# A node that hasn't answered after this many tries is given up on -- excluded
# from both the "first K replied" convergence check and the announce set.
SEARCH_NODE_MAX_PINGED = 3
# "The first K live nodes have replied" is what gates both convergence and
# announcing -- it's the same K as the replication factor, since those are
# exactly the nodes an announce needs to reach.
ANNOUNCE_LIVE_COUNT = K
# Safety cap on how many query rounds one `discover_and_announce_peers` call
# will run before giving up. `dht.c`'s own searches are long-lived and re-
# stepped forever (phase D territory here); this wrapper is still a bounded
# one-shot call, so it needs a ceiling -- generous, since each round is cheap
# once the candidate pool narrows to real nodes.
MAX_SEARCH_ROUNDS = 60

# --- `dht.c`'s node_good()/new_node() thresholds, verbatim ---
NODE_GOOD_PINGED_MAX = 2            # node_good(): pinged <= 2
NODE_GOOD_REPLY_WINDOW_S = 7200.0   # node_good(): reply_time >= now - 7200
NODE_GOOD_SEEN_WINDOW_S = 900.0     # node_good(): time >= now - 900 (15 min)
NODE_STALE_REFRESH_S = 900.0        # new_node(): n->time < now - 15*60 refreshes it
NODE_EVICT_PINGED = 3               # new_node(): pinged >= 3 makes a slot reusable
NODE_EVICT_IDLE_S = 15.0            # ...once 15s have passed since the last ping

# --- the filters that are the actual fix -- see the module docstring ---
# A candidate whose claimed ID is closer than this to the search target is
# rejected outright, wherever it was learned from. An honest node lands this
# close to a random 160-bit target with probability 2^-40; across the real
# ~8.4M-node DHT that's a false-positive risk of about 1e-5. Measured live:
# today's sybil fleet claims 2^36-2^78, comfortably inside this line.
IMPOSSIBLE_PROXIMITY_THRESHOLD = 1 << 120
# Inside this distance from a target, a claimed ID must also pass BEP 42
# verification (or be exempt -- RFC1918/loopback) or it is excluded, from
# both the search and the announce set. Outside it, a node is only ever used
# to route *toward* the target, so BEP 42 conformance isn't required -- about
# half the honest network (including Transmission's own nodes) predates BEP
# 42, and rejecting them there would throw away real routing capacity for no
# security benefit. Measured live: filtering this way took a real lookup from
# converging at 2^38 (sybils) to 2^138-2^142 (the genuine neighbourhood, 8/8
# verifying).
BEP42_CONTESTED_ZONE_THRESHOLD = 1 << 145

BLACKLIST_CAPACITY = 256  # dht.c uses a ring of 10; measured sybil fleets of
                           # 12+ distinct addresses in one lookup, so this
                           # gives real headroom without growing unbounded.

TOKEN_SIZE = 8                # dht.c TOKEN_SIZE
TOKEN_ROTATE_MIN_S = 900.0    # dht.c rotate_secrets(): 900 + random() % 1800
TOKEN_ROTATE_JITTER_S = 1800

RATE_LIMIT_CAPACITY = 400        # dht.c MAX_TOKEN_BUCKET_TOKENS
RATE_LIMIT_REFILL_PER_S = 100.0  # dht.c token_bucket(): 100 * elapsed seconds

PEER_EXPIRY_S = 32 * 60.0  # dht.c expire_storage(): 32 minutes


def load_node_cache(path) -> dict[Addr, bytes]:
    """Live DHT nodes learned by previous lookups, as {(ip, port): node_id}.

    Not an optimisation -- a correctness requirement. Only two of the
    well-known bootstrap routers still answer at all (measured: BitTorrent
    Inc's `router.bittorrent.com` and `router.utorrent.com` resolve but never
    reply), and the survivors rate-limit per source IP, so two cold lookups
    run back to back from one machine can leave the second with almost no
    seeds. Warm nodes make a lookup independent of whether a router feels
    like answering today.

    Every entry is re-validated here, not trusted -- `is_martian` and BEP 42
    on load. A cache file is exactly the poison's memory: it was measured at
    36 nodes with 26 of them BEP 42 invalid, meaning every restart re-seeded
    a lookup one hop inside the sybil trap instead of at a clean bootstrap
    router. A cache written by an older, unfiltered build must not be able to
    survive a restart into this one."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    cache: dict[Addr, bytes] = {}
    for item in raw if isinstance(raw, list) else []:
        try:
            node_id = bytes.fromhex(item["id"])
            ip = str(item["ip"])
            port = int(item["port"])
        except (KeyError, TypeError, ValueError):
            continue
        if len(node_id) != 20:
            continue
        if is_martian((ip, port)):
            continue
        if bep42_valid(node_id, ip) is False:  # None (exempt) still passes
            continue
        cache[(ip, port)] = node_id
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
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _size_socket_buffers(sock)
    sock.bind(("0.0.0.0", port))
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        # After bind, not before: applied to an unbound socket this silently
        # failed to take effect, and CI then showed the very error it is meant
        # to suppress -- "dht: socket error: ConnectionResetError(10054)",
        # with the lookup collapsing to 2/19 replied.
        #
        # And reported, not swallowed. The first version passed on `except
        # OSError: pass`, so a call that never worked looked identical to one
        # that did, and the resulting failure got misread as a timeout.
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)  # type: ignore[attr-defined]
        except OSError as exc:
            print(f"dht: could not disable UDP connection-reset reporting ({exc!r}); "
                  "discovery may stall on this machine", flush=True)
    sock.setblocking(False)
    return sock


# Transmission's sizes (tr-udp.cc: RecvBufferSize / SendBufferSize). A DHT
# node that answers queries receives in bursts, and the default buffer is small
# enough that a burst is simply dropped -- which looks from the inside exactly
# like an unreliable network, with no error anywhere to say otherwise.
RECV_BUFFER_BYTES = 4 * 1024 * 1024
SEND_BUFFER_BYTES = 1 * 1024 * 1024

# Below this the kernel has cut us down far enough to expect drops under load.
# Linux's default net.core.rmem_max (208 KB) is comfortably above it, so this
# stays quiet on an ordinary machine and speaks up on a squeezed one.
MIN_USEFUL_RECV_BUFFER = 128 * 1024


def _size_socket_buffers(sock: socket.socket) -> None:
    """Ask for big buffers, then check what we actually got.

    The check is the point, and it is why this mirrors Transmission rather than
    being a one-line setsockopt: every OS silently clamps to its own maximum,
    so a request that looks like it succeeded can leave a buffer a fraction of
    the size asked for.
    """
    for opt, wanted in ((socket.SO_RCVBUF, RECV_BUFFER_BYTES),
                        (socket.SO_SNDBUF, SEND_BUFFER_BYTES)):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, wanted)
        except OSError:
            pass
    try:
        got = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    except OSError:
        return
    if got < MIN_USEFUL_RECV_BUFFER:
        print(f"dht: receive buffer is only {got // 1024} KiB "
              f"(asked for {RECV_BUFFER_BYTES // 1024}); incoming packets may be dropped "
              "under load. On Linux, raise net.core.rmem_max.", flush=True)


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
    # Rejections, broken out by which filter caught them -- phase E's
    # diagnostics surface these; kept here (rather than bolted on later) so
    # a round that filtered out a whole sybil fleet doesn't just look like a
    # short candidate list with no explanation.
    rejected_martian: int = 0
    rejected_impossible_proximity: int = 0
    rejected_bep42: int = 0

    def summary(self) -> str:
        closest = "none" if self.closest_bits is None else f"2^{self.closest_bits}"
        return (f"{self.rounds} rounds, {self.replied}/{self.queried} replied, "
                f"closest {closest}, announced to {self.announced} "
                f"({self.no_token} gave no token), {self.peers_found} peer(s)")


def _prefix24(ip: str) -> str:
    """The /24 an address sits in -- the granularity libtorrent uses to decide
    two DHT nodes are not independent of each other."""
    return ip.rsplit(".", 1)[0]


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


# =============================================================================
# Phase A: pure functions -- BEP 42 node IDs, address hygiene, tokens.
# =============================================================================

_CRC32C_POLY = 0x82F63B78  # Castagnoli, reflected form


def _build_crc32c_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ _CRC32C_POLY if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_CRC32C_TABLE = _build_crc32c_table()


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli, polynomial 0x1EDC6F41 / reflected 0x82F63B78) --
    what BEP 42 hashes the masked IP into. This is *not* `zlib.crc32`, which
    uses the ISO/ANSI polynomial and silently gives a different, wrong
    result for every ID this feeds into."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


# BEP 42 -- DHT Security extension. Ties a node's ID to its IP so an attacker
# can't cheaply mint IDs clustered around a target: exactly what the live
# sybil fleet does (every forged ID shares the first 15 bytes of whatever
# info-hash is queried). Validated against the spec's own published test
# vectors (see tests/test_dht.py) -- not just against our own construction --
# and against the live network: filtering on this alone took a real lookup
# from converging at 2^38 (sybils) to 2^138-2^142 (the real neighbourhood).
#
# The spec's prose says the CRC is over "8 bytes of a big-endian 64-bit
# integer", but its own worked example hashes just the 4 masked IP octets --
# and 4 is what verifies against the published vectors *and* real nodes. We
# use 4.
_BEP42_V4_MASK = (0x03, 0x0F, 0x3F, 0xFF)


def _bep42_masked_ip(ip: str, r: int) -> bytes:
    octets = bytearray(socket.inet_aton(ip))
    for i in range(4):
        octets[i] &= _BEP42_V4_MASK[i]
    octets[0] |= (r << 5)
    return bytes(octets)


def _bep42_exempt(ip: str) -> bool:
    """RFC1918 / loopback / 0.x -- the spec exempts non-routable addresses,
    since a node behind one never had a routable IP to derive a conforming ID
    from in the first place."""
    octets = ip.split(".")
    if len(octets) != 4:
        return True
    a, b = int(octets[0]), int(octets[1])
    return a == 0 or a == 127 or a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def bep42_node_id(external_ip: str, seed: bytes) -> bytes:
    """Build a BEP-42-conforming ID for `external_ip`. The parts the spec
    leaves free -- the random 3 bits of id[2], the 16 middle bytes, and all of
    id[19] (the `rand` byte) -- are derived deterministically from `seed`
    (our Ed25519 pubkey) rather than actually randomised, so the ID is stable
    for a given identity on a given network: it has to survive a restart, or
    every restart re-bootstraps the routing table and every search from
    zero."""
    digest = hashlib.sha256(seed + b"roastmesh-bep42-node-id").digest()
    rand = digest[0]
    r = rand & 0x7
    crc = crc32c(_bep42_masked_ip(external_ip, r))
    node_id = bytearray(20)
    node_id[0] = (crc >> 24) & 0xFF
    node_id[1] = (crc >> 16) & 0xFF
    node_id[2] = ((crc >> 8) & 0xF8) | (digest[1] & 0x07)
    node_id[3:19] = digest[2:18]
    node_id[19] = rand
    return bytes(node_id)


def bep42_valid(node_id: bytes, ip: str) -> bool | None:
    """`None` means exempt (RFC1918/loopback/0.x) -- callers must not treat
    that as False. Checks `id[0]`, `id[1]`, and the top 5 bits of `id[2]`
    against what `bep42_node_id` would have produced for this IP and the
    `rand` byte the ID itself carries (`id[19]`)."""
    if len(node_id) != 20:
        return False
    if _bep42_exempt(ip):
        return None
    r = node_id[19] & 0x7
    crc = crc32c(_bep42_masked_ip(ip, r))
    expected0 = (crc >> 24) & 0xFF
    expected1 = (crc >> 16) & 0xFF
    expected2_top5 = (crc >> 8) & 0xF8
    return node_id[0] == expected0 and node_id[1] == expected1 and (node_id[2] & 0xF8) == expected2_top5


def is_martian(addr: Addr) -> bool:
    """`dht.c`'s `is_martian`, IPv4 branch, verbatim rules: port 0 (nothing
    could legitimately be listening), 0.x (unallocated), 127.x (loopback --
    a real remote peer is never legitimately there), and multicast/reserved
    (the top 3 bits of the first octet set, i.e. 224-255)."""
    ip, port = addr
    if port == 0:
        return True
    octets = ip.split(".")
    if len(octets) != 4:
        return True  # not a parseable IPv4 dotted-quad -- can't be legitimate
    first = int(octets[0])
    return first == 0 or first == 127 or (first & 0xE0) == 0xE0


class Blacklist:
    """LRU of misbehaving addresses. `dht.c` keeps a fixed ring of 10; we've
    measured sybil fleets of 12+ distinct addresses answering a single
    lookup, so 256 gives real headroom without growing unbounded."""

    def __init__(self, capacity: int = BLACKLIST_CAPACITY) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[Addr, None] = OrderedDict()

    def add(self, addr: Addr) -> None:
        self._entries.pop(addr, None)
        self._entries[addr] = None
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def __contains__(self, addr: object) -> bool:
        return addr in self._entries

    def __len__(self) -> int:
        return len(self._entries)


def make_token(addr: Addr, secret: bytes) -> bytes:
    """`dht.c`'s `make_token`: a keyed hash of the querying address, so a
    token handed to one address can't be replayed from another, without
    keeping any state per address that issued one."""
    ip, port = addr
    return hashlib.sha1(secret + socket.inet_aton(ip) + struct.pack(">H", port)).digest()[:TOKEN_SIZE]


def token_valid(token: bytes, addr: Addr, secret: bytes, previous_secret: bytes | None = None) -> bool:
    """`dht.c`'s `token_match`: accepts a token made with the *current*
    secret, or the previous one. Without the grace window, every token
    handed out in the seconds before a rotation is refused on the very
    `announce_peer` it was meant to authorize."""
    if len(token) != TOKEN_SIZE:
        return False
    if token == make_token(addr, secret):
        return True
    return previous_secret is not None and token == make_token(addr, previous_secret)


class TokenSecrets:
    """Owns the rotation schedule (`dht.c`'s `rotate_secrets`: every
    900 + rand(1800) seconds) around the pure `make_token`/`token_valid`
    functions above."""

    def __init__(self, *, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._secret = secrets.token_bytes(20)
        self._previous: bytes | None = None
        self._next_rotation = now + self._rotation_interval()

    @staticmethod
    def _rotation_interval() -> float:
        return TOKEN_ROTATE_MIN_S + secrets.randbelow(TOKEN_ROTATE_JITTER_S)

    def _maybe_rotate(self, now: float) -> None:
        if now >= self._next_rotation:
            self._previous = self._secret
            self._secret = secrets.token_bytes(20)
            self._next_rotation = now + self._rotation_interval()

    def make(self, addr: Addr, *, now: float | None = None) -> bytes:
        now = now if now is not None else time.monotonic()
        self._maybe_rotate(now)
        return make_token(addr, self._secret)

    def valid(self, token: bytes, addr: Addr, *, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        self._maybe_rotate(now)
        return token_valid(token, addr, self._secret, self._previous)


# =============================================================================
# Phase B: RoutingTable and Search, ported from dht.c, filters wired in at
# insertion -- see IMPOSSIBLE_PROXIMITY_THRESHOLD / BEP42_CONTESTED_ZONE_
# THRESHOLD above for why insertion (not the announce step) is where this has
# to happen.
# =============================================================================

@dataclass
class Node:
    id: bytes
    addr: Addr
    time: float = 0.0        # last time we confirmed it's actually at `addr`
    reply_time: float = 0.0  # last time it replied to a query of ours
    pinged: int = 0
    pinged_time: float = 0.0

    def good(self, now: float) -> bool:
        """`dht.c`'s `node_good()`, verbatim formula."""
        return (self.pinged <= NODE_GOOD_PINGED_MAX
                and self.reply_time >= now - NODE_GOOD_REPLY_WINDOW_S
                and self.time >= now - NODE_GOOD_SEEN_WINDOW_S)


# dht.c's scheme, restored after a live run showed why it matters: an unsplit
# bucket holds many nodes, and each split halves that down to a floor of K.
# Pinning every bucket at K instead looked like a harmless simplification and
# was not -- a fresh table filled its single bucket after 8 replies and turned
# every later one away, so after five minutes on the real DHT it held 15 nodes
# of which 3 were good. That starves the persisted state file, and that file is
# a correctness requirement rather than an optimisation (see load_node_cache):
# the surviving bootstrap routers rate-limit per source IP, so a node with no
# warm nodes of its own is one rate-limit away from no discovery at all.
BUCKET_MAX_COUNT = 128


class _Bucket:
    __slots__ = ("first", "max_count", "nodes")

    def __init__(self, first: int, max_count: int = BUCKET_MAX_COUNT) -> None:
        self.first = first  # inclusive lower bound of this bucket's ID range
        self.max_count = max_count
        self.nodes: list[Node] = []


def _lowbit(value: int) -> int:
    """`dht.c`'s `lowbit`: the MSB-numbered index (0 = the ID's very first
    bit) of the *lowest* set bit, or -1 for an all-zero ID. `bucket_middle`
    uses this to find where a bucket's range can be split in two without
    storing a prefix length anywhere -- it works because every bucket
    boundary that ever gets created is, by construction, a prefix followed
    by all-zero bits."""
    if value == 0:
        return -1
    trailing_zeros = (value & -value).bit_length() - 1
    return 159 - trailing_zeros


def _bucket_middle(first: int, next_first: int | None) -> int | None:
    bit = max(_lowbit(first), _lowbit(next_first) if next_first is not None else -1) + 1
    if bit >= 160:
        return None  # already at maximum depth -- can't split further
    return first | (1 << (159 - bit))


class RoutingTable:
    """`dht.c`'s routing table, IPv4-only (roastmesh never binds a v6 socket
    -- see `wan_discovery._resolve`'s reasoning): buckets over the 160-bit ID
    space, sorted by `first`, split only when the bucket holding our own ID
    fills up. `max_count` follows dht.c (see BUCKET_MAX_COUNT) -- the brief's
    simplification of `dht.c`'s "start at 128, halve on each split down to a
    floor of 8" scheme, which exists there to serve a global ~10^7-node
    table; roastmesh's is nowhere near that scale, so the extra headroom
    would buy nothing but complexity here."""

    def __init__(self, own_id: bytes, *, blacklist: Blacklist | None = None,
                 allow_loopback: bool = False,
                 on_dubious: Callable[[Node], None] | None = None) -> None:
        self.own_id = own_id
        self._own_id_int = int.from_bytes(own_id, "big")
        self._buckets: list[_Bucket] = [_Bucket(first=0, max_count=BUCKET_MAX_COUNT)]
        self.blacklist = blacklist if blacklist is not None else Blacklist()
        self._allow_loopback = allow_loopback
        self._on_dubious = on_dubious

    def _martian(self, addr: Addr) -> bool:
        # `is_martian` itself mirrors dht.c exactly, 127.x included, and is
        # unit-tested that way -- but a real remote attacker cannot get a
        # packet claiming source 127.x delivered to a socket bound to
        # 0.0.0.0 in the first place (the kernel drops it as martian before
        # any userspace code sees it), so the marginal protection this rule
        # buys a live node is close to nil. Defaulting to `allow_loopback`
        # is what lets every in-process test in this repo (this module's own
        # sybil swarm, and wan_discovery.py's fake-DHT tests, which bind
        # every node to 127.0.0.1) exercise the *real* admission path rather
        # than a stub -- pass allow_loopback=False for the strict dht.c rule.
        if self._allow_loopback and addr[0].startswith("127."):
            return False
        return is_martian(addr)

    def _bucket_index(self, id_int: int) -> int:
        lo, hi = 0, len(self._buckets) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._buckets[mid].first <= id_int:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _split(self, idx: int) -> bool:
        bucket = self._buckets[idx]
        next_first = self._buckets[idx + 1].first if idx + 1 < len(self._buckets) else None
        mid = _bucket_middle(bucket.first, next_first)
        if mid is None:
            return False
        # Halve on each split, floored at K -- so the space near our own ID is
        # held to Kademlia's replication factor while the far, coarse buckets
        # stay roomy enough to be worth persisting.
        bucket.max_count = max(bucket.max_count // 2, K)
        new_bucket = _Bucket(first=mid, max_count=bucket.max_count)
        kept, moved = [], []
        for n in bucket.nodes:
            (moved if int.from_bytes(n.id, "big") >= mid else kept).append(n)
        bucket.nodes = kept
        new_bucket.nodes = moved
        self._buckets.insert(idx + 1, new_bucket)
        return True

    def new_node(self, node_id: bytes, addr: Addr, *, confirm: int, now: float | None = None) -> Node | None:
        """`dht.c`'s `new_node(id, sa, salen, confirm)`, ported.

        `confirm`: 0 = learned indirectly (from someone else's `nodes` blob
        -- unconfirmed), 1 = we sent it a query (or it queried us) but
        haven't seen a reply yet, 2 = it just replied to us -- the only
        level that resets `pinged` and counts as a real liveness check.
        """
        now = now if now is not None else time.monotonic()
        if node_id == self.own_id:
            return None
        if self._martian(addr) or addr in self.blacklist:
            return None
        id_int = int.from_bytes(node_id, "big")
        idx = self._bucket_index(id_int)
        bucket = self._buckets[idx]

        # Dedupe by ID: a node that moved (NAT rebind, restart on a new port)
        # gets its address updated in place rather than creating a ghost
        # duplicate entry.
        for n in bucket.nodes:
            if n.id == node_id:
                if confirm or n.time < now - NODE_STALE_REFRESH_S:
                    n.addr = addr
                    if confirm:
                        n.time = now
                    if confirm >= 2:
                        n.reply_time = now
                        n.pinged = 0
                        n.pinged_time = 0.0
                return n

        # Reuse a known-bad slot before touching bucket capacity, so a
        # bucket kept saturated with dead nodes by a flood doesn't starve
        # out real ones that show up later.
        for n in bucket.nodes:
            if n.pinged >= NODE_EVICT_PINGED and n.pinged_time < now - NODE_EVICT_IDLE_S:
                n.id = node_id
                n.addr = addr
                n.time = now if confirm else 0.0
                n.reply_time = now if confirm >= 2 else 0.0
                n.pinged = 0
                n.pinged_time = 0.0
                return n

        if len(bucket.nodes) >= bucket.max_count:
            dubious = next((n for n in bucket.nodes
                             if not n.good(now) and n.pinged_time < now - NODE_EVICT_IDLE_S), None)
            if dubious is not None:
                # Ping it rather than evict it outright -- it might still be
                # alive, just quiet. The actual UDP ping is the caller's job
                # (this class has no socket); bumping the counters here is
                # what lets `on_dubious`, if given, know to send one.
                dubious.pinged += 1
                dubious.pinged_time = now
                if self._on_dubious is not None:
                    self._on_dubious(dubious)
                return None
            if self._bucket_index(self._own_id_int) == idx and self._split(idx):
                return self.new_node(node_id, addr, confirm=confirm, now=now)  # "goto again"
            return None  # bucket full, nothing to evict, not splittable here

        # One entry per IP in the whole table (libtorrent's
        # dht_restrict_routing_ips). Ports are free, addresses are not: a
        # single host must not be able to occupy several slots by binding
        # several sockets. BEP 42 already ties an ID to an address; this stops
        # one address spending that identity more than once.
        #
        # Exempt on non-routable addresses, the same set BEP 42 exempts. Several
        # nodes really do share one address there -- a household behind one NAT,
        # or an in-process test swarm on loopback -- and it is only on the public
        # internet that "many nodes, one address" is evidence of anything.
        if not _bep42_exempt(addr[0]) and self._has_other_node_at_ip(addr[0], node_id):
            return None

        node = Node(id=node_id, addr=addr, time=now if confirm else 0.0,
                    reply_time=now if confirm >= 2 else 0.0)
        bucket.nodes.append(node)
        return node

    def _has_other_node_at_ip(self, ip: str, node_id: bytes) -> bool:
        return any(n.addr[0] == ip and n.id != node_id
                   for b in self._buckets for n in b.nodes)

    def find(self, node_id: bytes) -> Node | None:
        idx = self._bucket_index(int.from_bytes(node_id, "big"))
        for n in self._buckets[idx].nodes:
            if n.id == node_id:
                return n
        return None

    def closest(self, target: bytes, count: int = K) -> list[Node]:
        target_int = int.from_bytes(target, "big")
        all_nodes = [n for b in self._buckets for n in b.nodes]
        all_nodes.sort(key=lambda n: int.from_bytes(n.id, "big") ^ target_int)
        return all_nodes[:count]

    def good_nodes(self, now: float | None = None) -> list[Node]:
        """The "good, diverse routing-table nodes" persistence is meant to
        save (see `load_node_cache`'s docstring) -- exposed for phase D's
        `wan_discovery` to build a cache dict from, instead of the old
        `stats.live_nodes` (the k-closest-to-one-target, i.e. exactly what a
        sybil fleet controls)."""
        now = now if now is not None else time.monotonic()
        return [n for b in self._buckets for n in b.nodes if n.good(now)]

    def __len__(self) -> int:
        return sum(len(b.nodes) for b in self._buckets)


@dataclass
class _SearchNode:
    id: bytes
    addr: Addr
    replied: bool = False
    reply_time: float = float("-inf")
    request_time: float = float("-inf")
    pinged: int = 0
    token: bytes | None = None
    acked: bool = False


class Search:
    """`dht.c`'s `struct search`, ported: `SEARCH_NODES` (14) slots, keyed by
    node ID -- not address, see `insert_search_node`'s `id_cmp` -- and kept
    sorted by XOR distance to `target`. This, plus the filters in `_admit`,
    is where the actual fix lives: the previous algorithm kept an 8-slot list
    keyed by *address* and inserted whatever answered, which is exactly the
    gap a sybil fleet (one address per forged ID, all answering instantly)
    walks straight through."""

    def __init__(self, target: bytes, *, blacklist: Blacklist | None = None,
                 allow_loopback: bool = False) -> None:
        self.target = target
        self._target_int = int.from_bytes(target, "big")
        self.blacklist = blacklist if blacklist is not None else Blacklist()
        self._allow_loopback = allow_loopback
        self._nodes: list[_SearchNode] = []
        self._by_id: dict[bytes, _SearchNode] = {}
        self.found: set[Addr] = set()
        self._prefix_counts: dict[str, int] = {}
        # Rejection tally, broken out by cause -- see LookupStats' fields of
        # the same name, which the driver copies these into.
        self.rejected_martian = 0
        self.rejected_impossible_proximity = 0
        self.rejected_bep42 = 0
        self.rejected_prefix = 0

    def _dist(self, node_id: bytes) -> int:
        return int.from_bytes(node_id, "big") ^ self._target_int

    def _martian(self, addr: Addr) -> bool:
        # See RoutingTable._martian for why loopback is allowed by default.
        if self._allow_loopback and addr[0].startswith("127."):
            return False
        return is_martian(addr)

    def _admit(self, node_id: bytes, addr: Addr) -> bool:
        """The fix, stated as code: reject before the candidate can ever
        enter `_nodes` (and so before it can ever contribute `values` or
        receive an announce -- both only ever look at admitted nodes)."""
        if self._martian(addr) or addr in self.blacklist:
            self.rejected_martian += 1
            return False
        d = self._dist(node_id)
        if d < IMPOSSIBLE_PROXIMITY_THRESHOLD:
            # A forged-proximity claim, not noise -- blacklist it, the same
            # as a malformed `nodes` blob or an address that changes its
            # claimed ID (see DhtClient._ingest_reply).
            self.blacklist.add(addr)
            self.rejected_impossible_proximity += 1
            return False
        if d < BEP42_CONTESTED_ZONE_THRESHOLD and bep42_valid(node_id, addr[0]) is False:
            self.rejected_bep42 += 1
            return False
        if (not _bep42_exempt(addr[0])
                and self._prefix_counts.get(_prefix24(addr[0]), 0) >= MAX_SEARCH_NODES_PER_PREFIX):
            self.rejected_prefix += 1
            return False
        return True

    def insert(self, node_id: bytes, addr: Addr, *, replied: bool, token: bytes | None = None) -> _SearchNode | None:
        if not self._admit(node_id, addr):
            return None
        existing = self._by_id.get(node_id)
        if existing is None:
            if len(self._nodes) >= SEARCH_NODES and self._dist(node_id) >= self._dist(self._nodes[-1].id):
                return None  # farther than everyone already holding a slot
            node = _SearchNode(id=node_id, addr=addr)
            self._by_id[node_id] = node
            self._nodes.append(node)
            self._prefix_counts[_prefix24(addr[0])] = \
                self._prefix_counts.get(_prefix24(addr[0]), 0) + 1
            self._nodes.sort(key=lambda n: self._dist(n.id))
            if len(self._nodes) > SEARCH_NODES:
                worst = self._nodes.pop()
                del self._by_id[worst.id]
                # Give the slot back to its network, or a search that churns
                # through candidates would slowly lock every /24 out.
                prefix = _prefix24(worst.addr[0])
                self._prefix_counts[prefix] = max(self._prefix_counts.get(prefix, 1) - 1, 0)
        else:
            node = existing
            node.addr = addr
        if replied:
            node.replied = True
            node.reply_time = time.monotonic()
            node.request_time = float("-inf")
            node.pinged = 0
        if token is not None:
            node.token = token
        self._nodes.sort(key=lambda n: self._dist(n.id))
        return node

    def discard(self, node_id: bytes | None, addr: Addr) -> None:
        """Purge a node the caller just blacklisted (malformed `nodes` blob,
        forged-proximity, identity swap) out of this search, mirroring
        `dht.c`'s `blacklist_node` flushing it from every in-progress
        search."""
        if node_id is not None:
            node = self._by_id.pop(node_id, None)
            if node is not None and node in self._nodes:
                self._nodes.remove(node)

    def nodes_to_query(self, now: float, limit: int = DHT_INFLIGHT_QUERIES) -> list[_SearchNode]:
        out = []
        for n in self._nodes:
            if len(out) >= limit:
                break
            if n.pinged >= SEARCH_NODE_MAX_PINGED or n.replied:
                continue
            if n.request_time > now - DHT_SEARCH_RETRANSMIT_S:
                continue
            out.append(n)
        return out

    def mark_queried(self, node: _SearchNode, *, now: float) -> None:
        node.pinged += 1
        node.request_time = now

    def first_k_replied(self, k: int = ANNOUNCE_LIVE_COUNT) -> bool:
        """`search_step`'s "check if the first 8 live nodes have replied":
        walk the (distance-sorted) list, skipping maxed-out nodes, and
        require the first `k` counted to have replied. This is what gates
        both convergence and moving on to announce."""
        j = 0
        for n in self._nodes:
            if n.pinged >= SEARCH_NODE_MAX_PINGED:
                continue
            if not n.replied:
                return False
            j += 1
            if j >= k:
                break
        return True

    def exhausted(self) -> bool:
        """Nothing left worth waiting on: every candidate has either replied
        or been given up on."""
        return all(n.replied or n.pinged >= SEARCH_NODE_MAX_PINGED for n in self._nodes)

    def announce_targets(self, k: int = ANNOUNCE_LIVE_COUNT) -> list[_SearchNode]:
        """The first `k` live (not maxed-out) nodes, in distance order -- the
        set `search_step` announces to once `first_k_replied()` is true."""
        out = []
        for n in self._nodes:
            if n.pinged >= SEARCH_NODE_MAX_PINGED:
                continue
            out.append(n)
            if len(out) >= k:
                break
        return out


# =============================================================================
# Phase C: serving -- rate limiting and peer storage.
# =============================================================================

# libtorrent's dht_max_peers_reply. Also bounds our amplification factor: a
# get_peers reply is bigger than the query that provokes it, and an unbounded
# one is a gift to anyone spoofing a source address.
MAX_PEERS_REPLY = 100

# libtorrent's dht_restrict_search_ips: candidates too close together in CIDR
# terms are not independent, so one network should not supply a search's whole
# frontier. Two per /24 leaves room for an ordinary pair of nodes behind one
# ISP block while making a rented /24 useless for surrounding a target. Like
# the routing-table rule, it does not apply to non-routable addresses.
MAX_SEARCH_NODES_PER_PREFIX = 2


# libtorrent's ip_voter, in the shape we need. Its numbers: rotate the tally
# once ~50 votes are in or five minutes have passed, and require a clear
# majority -- not a plurality -- before displacing an address we had already
# settled on.
IP_VOTE_ROTATE_AFTER = 50
IP_VOTE_ROTATE_S = 300.0
IP_VOTE_QUORUM = 3


class IpVoter:
    """What other nodes say our external address is, with an expiry date.

    BEP 42 asks every node to echo the querier's address back, and that is the
    only way a NAT'd node can learn its own. But a tally that only ever grows
    is a trap: after a laptop changes network, a VPN reconnects, or a DHCP
    lease turns over, the old address keeps an unbeatable lead forever. We then
    publish an address we no longer hold and never re-derive the BEP 42 node ID
    that depends on it -- silently, because every internal check still agrees
    with itself.

    So the tally is rotated, exactly as libtorrent does: the round's winner is
    remembered, the votes are cleared, and the next round gets to disagree.
    Hysteresis keeps that from flapping -- to unseat a settled address a
    challenger needs twice the runner-up's votes, so one or two liars cannot
    move us.
    """

    def __init__(self, *, quorum: int = IP_VOTE_QUORUM,
                 rotate_after: int = IP_VOTE_ROTATE_AFTER,
                 rotate_s: float = IP_VOTE_ROTATE_S, now: float | None = None) -> None:
        self._quorum = quorum
        self._rotate_after = rotate_after
        self._rotate_s = rotate_s
        self._votes: dict[Addr, set[Addr]] = {}
        self._settled: Addr | None = None
        self._prev_ports: set[int] = set()
        self._started = now if now is not None else time.monotonic()

    def record(self, responder: Addr, claimed: Addr, *, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._maybe_rotate(now)
        # One vote per responder per round, so a single chatty node cannot
        # stuff the ballot by answering repeatedly.
        self._votes.setdefault(claimed, set()).add(responder)

    @property
    def tally(self) -> dict[Addr, int]:
        return {claim: len(responders) for claim, responders in self._votes.items()}

    def ports_seen(self) -> set[int]:
        """Ports claimed this round *and* last.

        Spanning two rounds on purpose: the symmetric-NAT verdict is "different
        responders see different ports", and reading it from a tally that was
        just cleared would report a freshly-rotated symmetric node as having a
        consistent mapping until the next few votes arrive.
        """
        return {port for _ip, port in self._votes} | self._prev_ports

    def current(self, *, now: float | None = None) -> Addr | None:
        now = now if now is not None else time.monotonic()
        self._maybe_rotate(now)
        return self._winner() or self._settled

    def _maybe_rotate(self, now: float) -> None:
        total = sum(len(v) for v in self._votes.values())
        if not self._votes:
            return
        if total < self._rotate_after and now - self._started < self._rotate_s:
            return
        winner = self._winner()
        if winner is not None:
            self._settled = winner
        self._prev_ports = {port for _ip, port in self._votes}
        self._votes.clear()
        self._started = now

    def _winner(self) -> Addr | None:
        if not self._votes:
            return None
        ranked = sorted(self._votes.items(), key=lambda kv: len(kv[1]), reverse=True)
        top, top_votes = ranked[0][0], len(ranked[0][1])
        if top_votes < self._quorum:
            return None
        runner_up = len(ranked[1][1]) if len(ranked) > 1 else 0
        if self._settled is not None and top != self._settled and top_votes <= runner_up * 2:
            return self._settled  # not a clear enough majority to move us
        return top


# libtorrent's dos_blocker: 5 messages/second sustained per address, and a
# source that manages 50 inside ten seconds is ignored for five minutes.
SOURCE_RATE_LIMIT = 5
SOURCE_BURST_WINDOW_S = 10.0
SOURCE_BLOCK_S = 300.0
SOURCE_TRACK_LIMIT = 4096


class SourceLimiter:
    """Per-address request limiting, which the global bucket cannot do.

    A single token bucket answers "are we being asked too much", never "by
    whom". One noisy source can drain all 400 tokens and every other node on
    the network is refused service as a result -- and now that we answer
    queries at all, that is a stranger's decision to make about us. libtorrent
    keeps both: a global budget and a per-source one.
    """

    def __init__(self, *, rate: int = SOURCE_RATE_LIMIT,
                 window_s: float = SOURCE_BURST_WINDOW_S,
                 block_s: float = SOURCE_BLOCK_S) -> None:
        self._rate = rate
        self._window_s = window_s
        self._block_s = block_s
        self._seen: dict[str, list[float]] = {}   # ip -> [count, window_start, blocked_until]

    def allow(self, ip: str, *, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        entry = self._seen.get(ip)
        if entry is None:
            if len(self._seen) >= SOURCE_TRACK_LIMIT:
                self._evict(now)
            self._seen[ip] = [1.0, now, 0.0]
            return True

        count, window_start, blocked_until = entry
        if now < blocked_until:
            return False
        if now - window_start > self._window_s:
            # The burst took longer than the window, so it was not a burst.
            entry[0], entry[1] = 1.0, now
            return True
        count += 1
        entry[0] = count
        if count >= self._rate * self._window_s:
            entry[2] = now + self._block_s
            return False
        return True

    def _evict(self, now: float) -> None:
        """Keep the table bounded. Blocked addresses are exactly the ones worth
        remembering, so they survive; everyone else is cheap to re-learn."""
        for ip, (_count, window_start, blocked_until) in list(self._seen.items()):
            if now >= blocked_until and now - window_start > self._window_s:
                del self._seen[ip]
        if len(self._seen) >= SOURCE_TRACK_LIMIT:
            self._seen.clear()


class TokenBucket:
    """`dht.c`'s rate limiter for incoming *requests*: 400 tokens, refilled
    at 100/s, so a burst can spend up to 400 at once but sustained traffic is
    capped at 100 requests/s. This is what makes it safe to answer strangers
    at all -- a `get_peers` reply is bigger than the query that provokes it,
    so an unthrottled responder is a free amplifier."""

    def __init__(self, *, capacity: int = RATE_LIMIT_CAPACITY,
                 refill_per_s: float = RATE_LIMIT_REFILL_PER_S, now: float | None = None) -> None:
        self._capacity = capacity
        self._refill_per_s = refill_per_s
        self._tokens = capacity
        self._last = now if now is not None else time.monotonic()

    def take(self, *, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        if self._tokens <= 0:
            # dht.c only checks the clock once the bucket is actually empty,
            # not continuously -- replicated verbatim rather than "improved"
            # into a continuous accrual, since the brief is to mirror dht.c.
            elapsed = max(now - self._last, 0.0)
            self._tokens = min(self._capacity, int(self._refill_per_s * elapsed))
            self._last = now
        if self._tokens <= 0:
            return False
        self._tokens -= 1
        return True


class PeerStore:
    """`dht.c`'s `storage`/`storage_store`/`expire_storage`: peers announced
    to *us*, per info-hash, expiring after 32 minutes -- the only reason we
    can ever answer `get_peers` with `values` instead of just `nodes`."""

    def __init__(self) -> None:
        self._peers: dict[bytes, dict[Addr, float]] = {}

    def store(self, info_hash: bytes, addr: Addr, *, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._peers.setdefault(info_hash, {})[addr] = now

    def get(self, info_hash: bytes, *, now: float | None = None) -> list[Addr]:
        now = now if now is not None else time.monotonic()
        self._expire_one(info_hash, now=now)
        bucket = self._peers.get(info_hash)
        return list(bucket) if bucket else []

    def expire(self, *, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        for info_hash in list(self._peers):
            self._expire_one(info_hash, now=now)

    def _expire_one(self, info_hash: bytes, *, now: float) -> None:
        bucket = self._peers.get(info_hash)
        if not bucket:
            return
        for addr in [a for a, t in bucket.items() if t < now - PEER_EXPIRY_S]:
            del bucket[addr]
        if not bucket:
            del self._peers[info_hash]


def _encode_nodes(nodes: list[Node]) -> bytes:
    return b"".join(n.id + encode_compact_addr(n.addr) for n in nodes)


def _as_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 0
    return port if 0 < port <= 65535 else 0


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
    its hello to (see wan_discovery).

    Holds a long-lived `routing_table` (general routing knowledge, and what
    answers other nodes' `find_node`/`get_peers`) separately from the
    short-lived `Search` objects `discover_and_announce_peers` creates one of
    per lookup -- exactly `dht.c`'s split between its persistent buckets and
    its per-target `struct search`."""

    def __init__(self, transport: asyncio.DatagramTransport, own_id: bytes,
                 *, allow_loopback: bool = False) -> None:
        self._transport = transport
        self._setup(own_id, allow_loopback)

    def _setup(self, own_id: bytes, allow_loopback: bool) -> None:
        self.own_id = own_id
        self.on_foreign_datagram: Callable[[bytes, Addr], None] | None = None
        self._pending: dict[bytes, asyncio.Future] = {}
        self._next_t = 0
        self._allow_loopback = allow_loopback
        self.blacklist = Blacklist()
        self.routing_table = RoutingTable(own_id, blacklist=self.blacklist, allow_loopback=allow_loopback)
        self._tokens = TokenSecrets()
        self._rate_limiter = TokenBucket()
        self._source_limiter = SourceLimiter()
        self._peer_store = PeerStore()
        # addr -> the last node ID we saw claim it. A real node's address can
        # change (NAT rebind) and RoutingTable/Search both handle that by
        # dedupe-by-ID; the opposite -- one address suddenly claiming a
        # *different* ID -- has no honest explanation, so it's blacklisted
        # instead of just updated. dht.c has no equivalent of this check (it
        # keys purely by ID and lets addresses move freely); it's added here
        # because that asymmetry is exactly what a sybil holding one address
        # could exploit to keep rotating identity against a single socket.
        self._known_identity: dict[Addr, bytes] = {}
        # {claimed (ip, port): {addr of each distinct responder who claimed it}}
        # -- phase D's external-address/NAT detection reads this; nothing
        # here acts on it yet.
        self._ip_voter = IpVoter()

    @classmethod
    async def bind(cls, *, port: int, own_id: bytes, allow_loopback: bool = False) -> "DhtClient":
        loop = asyncio.get_running_loop()
        client = cls.__new__(cls)
        client._setup(own_id, allow_loopback)
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _DhtProtocol(client), sock=udp_socket(port),
        )
        client._transport = transport
        return client

    @property
    def ip_votes(self) -> dict[Addr, int]:
        return self._ip_voter.tally

    @property
    def external_address(self) -> Addr | None:
        """Our public address as the network reports it, or None until enough
        distinct nodes agree. See IpVoter -- this expires, deliberately."""
        return self._ip_voter.current()

    @property
    def external_ports_seen(self) -> set[int]:
        return self._ip_voter.ports_seen()

    def send_datagram(self, data: bytes, addr: Addr) -> None:
        # A send on a closed transport does not raise something catchable --
        # asyncio's datagram transport tries to report the error through a
        # loop reference it has already dropped, and the result is an
        # AttributeError out of selector_events that looks like a DHT bug and
        # is really just teardown ordering. Background senders (wan_discovery's
        # bootstrap drip, hello retries) race socket close by construction, so
        # the guard belongs here rather than at each call site.
        if self._transport.is_closing():
            return
        self._transport.sendto(data, addr)

    def close(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._transport.close()

    def _record_ip_vote(self, responder: Addr, raw_ip: object) -> None:
        if not isinstance(raw_ip, bytes) or len(raw_ip) != 6:
            return
        claimed = (socket.inet_ntoa(raw_ip[:4]), struct.unpack(">H", raw_ip[4:6])[0])
        self._ip_voter.record(responder, claimed)

    def adopt_node_id(self, new_id: bytes) -> bool:
        """Switch to a different node ID, starting the routing table over.

        BEP 42 ties a conforming node ID to the external IP, which we can only
        learn *after* some replies have come back (see `ip_votes`) -- so the
        first ID is provisional by necessity, and this is how the real one gets
        taken up once the votes agree.

        The routing table is rebuilt rather than carried over: every bucket
        boundary in it was chosen relative to the old ID, and buckets only ever
        split around the one containing our own, so keeping it would leave the
        table split around a point we no longer occupy. The blacklist survives
        deliberately -- who lied to us does not depend on what we call
        ourselves.
        """
        if len(new_id) != 20 or new_id == self.own_id:
            return False
        self.own_id = new_id
        self.routing_table = RoutingTable(new_id, blacklist=self.blacklist,
                                          allow_loopback=self._allow_loopback)
        return True

    async def bootstrap_ping(self, addr: Addr, *, timeout: float = 4.0) -> bool:
        """Ping an address and, if it answers, admit it to the routing table.

        A bare `ping` deliberately does not touch the table -- a reply is
        matched to its future and nothing else -- but bootstrapping is the one
        case where the reply *is* the point. Without this the table has no way
        to get its first entries before a search has run, and a search that
        starts from an empty table is exactly the cold, router-dependent lookup
        this rewrite exists to stop relying on.
        """
        reply = await self.ping(addr, timeout=timeout)
        if not isinstance(reply, dict):
            return False
        node_id = reply.get(b"id")
        if not (isinstance(node_id, bytes) and len(node_id) == 20):
            return False
        if not self._identity_ok(node_id, addr):
            return False
        return self.routing_table.new_node(node_id, addr, confirm=2) is not None

    def _identity_ok(self, node_id: bytes, addr: Addr) -> bool:
        prior = self._known_identity.get(addr)
        if prior is not None and prior != node_id:
            self.blacklist.add(addr)
            return False
        self._known_identity[addr] = node_id
        return True

    def _reply(self, t, r: dict, addr: Addr) -> None:
        # The querier's external address, echoed back as a `ip` key in the
        # TOP-LEVEL dictionary, beside "t"/"y"/"r" -- not inside "r". BEP 42 is
        # emphatic about this ("It is important that the ip field is in the top
        # level dictionary"), and it is how a node with no other way to learn
        # its public address bootstraps a conforming node ID. Putting it inside
        # "r" is invisible to every real client, and the mistake hides itself:
        # our own reader looked in the same wrong place, so client and server
        # agreed perfectly with each other and with nothing else on the network.
        try:
            self.send_datagram(bencode({
                b"t": t or b"", b"y": b"r", b"r": r,
                b"ip": encode_compact_addr(addr),
            }), addr)
        except OSError:
            pass

    def _send_error(self, t, code: int, message: str, addr: Addr) -> None:
        try:
            self.send_datagram(bencode({b"t": t or b"", b"y": b"e", b"e": [code, message]}), addr)
        except OSError:
            pass

    def _handle_message(self, msg: dict, addr: Addr) -> None:
        y = msg.get(b"y")
        t = msg.get(b"t")
        if y in (b"r", b"e"):
            if y == b"r":
                # Top level, not inside `r`. BEP 42: "It is important that the
                # ip field is in the top level dictionary." Reading it from `r`
                # silently found nothing -- measured against the live DHT, where
                # three separate nodes were in fact reporting it correctly.
                self._record_ip_vote(addr, msg.get(b"ip"))
            fut = self._pending.pop(t, None) if t is not None else None
            if fut is not None and not fut.done():
                fut.set_result(msg.get(b"r") if y == b"r" else None)
            return
        if y != b"q":
            return  # not a well-formed DHT message -- ignore

        q = msg.get(b"q")
        a = msg.get(b"a") or {}
        querier_id = a.get(b"id")
        if not (isinstance(querier_id, bytes) and len(querier_id) == 20) or querier_id == self.own_id:
            return
        # Rate-limit *requests* only -- dht.c gates every incoming query this
        # way, but never gates replies to our own outgoing queries (a flood
        # of unanswered requests must not also be able to starve our own
        # lookups).
        if not self._rate_limiter.take():
            return
        # Per-source as well as global: without this one address can spend the
        # whole global budget and everyone else is refused on its behalf.
        if not self._source_limiter.allow(addr[0]):
            return
        if not self._identity_ok(querier_id, addr):
            return
        # confirm=1: we've heard from it directly, but (unlike a reply to our
        # own query) it hasn't demonstrated liveness *to a query we sent*.
        self.routing_table.new_node(querier_id, addr, confirm=1)

        if q == b"ping":
            self._reply(t, {b"id": self.own_id}, addr)
        elif q == b"find_node":
            target = a.get(b"target")
            if not (isinstance(target, bytes) and len(target) == 20):
                return
            nodes = self.routing_table.closest(target, K)
            self._reply(t, {b"id": self.own_id, b"nodes": _encode_nodes(nodes)}, addr)
        elif q == b"get_peers":
            info_hash = a.get(b"info_hash")
            if not (isinstance(info_hash, bytes) and len(info_hash) == 20):
                self._send_error(t, 203, "get_peers with no info_hash", addr)
                return
            token = self._tokens.make(addr)
            reply = {b"id": self.own_id, b"token": token}
            held = self._peer_store.get(info_hash)
            if held:
                reply[b"values"] = [encode_compact_addr(p) for p in held[:MAX_PEERS_REPLY]]
            else:
                reply[b"nodes"] = _encode_nodes(self.routing_table.closest(info_hash, K))
            self._reply(t, reply, addr)
        elif q == b"announce_peer":
            info_hash = a.get(b"info_hash")
            token = a.get(b"token")
            if not (isinstance(info_hash, bytes) and len(info_hash) == 20):
                self._send_error(t, 203, "announce_peer with no info_hash", addr)
                return
            if not (isinstance(token, bytes) and self._tokens.valid(token, addr)):
                self._send_error(t, 203, "announce_peer with wrong token", addr)
                return
            port = addr[1] if a.get(b"implied_port") else _as_port(a.get(b"port"))
            if port:
                self._peer_store.store(info_hash, (addr[0], port))
            self._reply(t, {b"id": self.own_id}, addr)
        # anything else is silently ignored, as before.

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

    async def find_node(self, addr: Addr, target: bytes, *, timeout: float = 4.0) -> dict | None:
        return await self._query(
            addr, "find_node", {b"id": self.own_id, b"target": target}, timeout=timeout,
        )

    async def get_peers(self, addr: Addr, info_hash: bytes, *, timeout: float = 4.0) -> dict | None:
        return await self._query(
            addr, "get_peers", {b"id": self.own_id, b"info_hash": info_hash}, timeout=timeout,
        )

    async def announce_peer(self, addr: Addr, info_hash: bytes, token: bytes, *,
                            timeout: float = 4.0, public_port: int | None = None) -> dict | None:
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
        if public_port is not None:
            # `implied_port=0` means "store the port I am telling you", which is
            # the only correct answer when a router or VPN forwards a fixed port
            # to us. Behind such a forward the source port is *not* the reachable
            # one: measured on a Raspberry Pi with PIA port forwarding, inbound
            # to the forwarded port arrived perfectly while outbound packets from
            # that same socket left with a fresh random source port every time.
            # implied_port there publishes an address nobody can use.
            return await self._query(addr, "announce_peer", {
                b"id": self.own_id, b"info_hash": info_hash, b"port": int(public_port),
                b"implied_port": 0, b"token": token,
            }, timeout=timeout)
        return await self._query(addr, "announce_peer", {
            b"id": self.own_id, b"info_hash": info_hash, b"port": local_port,
            b"implied_port": 1, b"token": token,
        }, timeout=timeout)

    # -- Search driving: the filters live in Search/RoutingTable; this is
    # just the round loop that feeds them wire replies. ---------------------

    def _blacklist_and_purge(self, node_id: bytes | None, addr: Addr, search: Search) -> None:
        """`dht.c`'s `blacklist_node`: blacklist the address, mark any
        routing-table entry for that ID evictable, and flush it out of the
        search in progress -- used for a malformed `nodes` blob and (via
        `_identity_ok`, from the caller) an address that swapped IDs."""
        self.blacklist.add(addr)
        if node_id is not None:
            rt_node = self.routing_table.find(node_id)
            if rt_node is not None:
                rt_node.pinged = NODE_EVICT_PINGED
        search.discard(node_id, addr)

    def _ingest_reply(self, search: Search, addr: Addr, resp: object, *, stats: LookupStats) -> None:
        if not isinstance(resp, dict):
            return  # timeout, error reply, or a gathered exception
        stats.replied += 1
        node_id = resp.get(b"id")
        if not (isinstance(node_id, bytes) and len(node_id) == 20):
            return
        if not self._identity_ok(node_id, addr):
            return

        raw_nodes = resp.get(b"nodes")
        if isinstance(raw_nodes, bytes) and raw_nodes and len(raw_nodes) % 26 != 0:
            # Not "slightly wrong" -- a broken or hostile implementation.
            # Blacklist rather than try to salvage a partial parse.
            self._blacklist_and_purge(node_id, addr, search)
            return
        candidates = decode_compact_nodes(raw_nodes) if isinstance(raw_nodes, bytes) else []

        self.routing_table.new_node(node_id, addr, confirm=2)
        token = resp.get(b"token")
        node = search.insert(node_id, addr, replied=True, token=token if isinstance(token, bytes) else None)
        if node is None:
            # `_admit` blacklists a forged-proximity claim, and a node that
            # lies about its distance lies for every target -- so evict the
            # routing-table entry `new_node` just made, rather than leaving it
            # to re-seed the next search. A BEP 42 rejection is target-relative
            # and deliberately leaves the routing table alone.
            if addr in self.blacklist:
                self._blacklist_and_purge(node_id, addr, search)
            return  # rejected at insertion -- its values/nodes are not trusted

        for raw in resp.get(b"values") or []:
            if isinstance(raw, bytes):
                search.found.update(a for a in decode_compact_peers(raw) if a != ("0.0.0.0", 0))

        for cand_id, cand_addr in candidates:
            if cand_id == node_id and cand_addr == addr:
                continue
            search.insert(cand_id, cand_addr, replied=False)
            self.routing_table.new_node(cand_id, cand_addr, confirm=0)

    async def _query_and_ingest_addrs(self, search: Search, addrs: list[Addr], *,
                                       timeout: float, stats: LookupStats) -> None:
        """The bootstrap round: raw addresses whose ID we don't know yet
        (bootstrap routers, a discard-port test seed), queried directly --
        `Search.insert` needs an ID before it can track anything, so these
        can't go through `nodes_to_query` until they've answered once."""
        replies = await asyncio.gather(
            *(self.get_peers(a, search.target, timeout=timeout) for a in addrs),
            return_exceptions=True,
        )
        for addr, resp in zip(addrs, replies):
            stats.queried += 1
            self._ingest_reply(search, addr, resp, stats=stats)

    async def _query_and_ingest_nodes(self, search: Search, nodes: list[_SearchNode], *,
                                       timeout: float, stats: LookupStats) -> None:
        now = time.monotonic()
        for n in nodes:
            search.mark_queried(n, now=now)
        replies = await asyncio.gather(
            *(self.get_peers(n.addr, search.target, timeout=timeout) for n in nodes),
            return_exceptions=True,
        )
        for n, resp in zip(nodes, replies):
            stats.queried += 1
            self._ingest_reply(search, n.addr, resp, stats=stats)

    async def discover_and_announce_peers(
        self, info_hash: bytes, bootstrap_nodes: list[Addr], *,
        timeout: float = 4.0, announce: bool = True,
        seed_ids: dict[Addr, bytes] | None = None,
        stats: LookupStats | None = None,
        announce_if: Callable[[LookupStats], bool] | None = None,
        public_port: int | None = None,
    ) -> set[Addr]:
        """A thin wrapper over `Search`, kept so `wan_discovery.py` and
        `cli.py`'s existing call sites -- both call this as a bound method
        with exactly these keywords -- don't have to change for phases A-C.
        Phase D replaces this with a long-lived, continuously-stepped search;
        this bounded version builds one `Search`, drives it to convergence
        (first-K-live-replied) and then to fully acked (if announcing), and
        returns what it found.

        Every candidate that reaches `found`, `stats.live_nodes`, or the
        announce set has passed `Search._admit` -- martian/blacklist,
        impossible-proximity, and (inside the contested zone) BEP 42. That
        filtering, not anything in this method, is the actual fix; see the
        module docstring.
        """
        stats = stats if stats is not None else LookupStats()
        search = Search(info_hash, blacklist=self.blacklist, allow_loopback=self._allow_loopback)
        seed_ids = seed_ids or {}

        # Bootstrap round: routers and any address we don't already have a
        # cached ID for. Cached seeds skip straight to being ranked by their
        # real (already-known) distance instead of being queried blind.
        unknown = [a for a in dict.fromkeys(bootstrap_nodes) if a not in seed_ids]
        stats.seeds_used = len(unknown) + len(seed_ids)
        if unknown:
            stats.rounds += 1
            await self._query_and_ingest_addrs(search, unknown, timeout=timeout, stats=stats)
        for addr, node_id in seed_ids.items():
            search.insert(node_id, addr, replied=False)

        for _round in range(MAX_SEARCH_ROUNDS):
            if search.first_k_replied():
                break
            now = time.monotonic()
            batch = search.nodes_to_query(now)
            if not batch:
                if search.exhausted():
                    break
                # Nothing eligible this instant (everything's within its
                # retransmit window) but not exhausted either -- give
                # in-flight timers a moment rather than busy-looping.
                await asyncio.sleep(0.05)
                continue
            stats.rounds += 1
            await self._query_and_ingest_nodes(search, batch, timeout=timeout, stats=stats)

        live = [n for n in search.announce_targets() if n.replied]
        if live:
            stats.closest_bits = max(search._dist(live[0].id).bit_length() - 1, 0)
        stats.live_nodes = [(n.addr, n.id) for n in live]
        stats.rejected_martian = search.rejected_martian
        stats.rejected_impossible_proximity = search.rejected_impossible_proximity
        stats.rejected_bep42 = search.rejected_bep42

        # `announce_if` is checked here, not by the caller, because only now is
        # the thing worth deciding on known: how close the walk actually got.
        # A caller that decides up front can only ask "am I due?", and a lookup
        # that never reached the target neighbourhood publishes us to nodes
        # nobody will ever ask.
        if announce and (announce_if is None or announce_if(stats)):
            for n in live:
                if n.token is None:
                    stats.no_token += 1
                    continue
                if await self.announce_peer(n.addr, info_hash, n.token, timeout=timeout,
                                            public_port=public_port) is not None:
                    n.acked = True
                    stats.announced += 1

        stats.peers_found = len(search.found)
        return set(search.found)
