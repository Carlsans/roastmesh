"""The one-datagram "here's my feed and ticket" announcement, shared by
lan_discovery (broadcast) and wan_discovery (DHT-rendezvous'd unicast) --
same wire format either way, so a node discovered over the internet is
handled by exactly the same code path as one discovered on the LAN.

Also doubles as the pairing beacon (lan_discovery.discover_pairing_beacons):
the same datagram, with `pairing`/`code`/`hostname` added, is what two
devices trying to pair broadcast and listen for to find each other on the
LAN -- see pairing.py for what happens once they do. Kept as one wire format
rather than a second one specifically so the always-on discovery beacon and
the pairing beacon can share every byte of socket/multicast/per-interface
plumbing (_join_multicast, _announce) with zero duplication, and so an
old build's plain discovery beacon and a new build's pairing beacon are
trivially distinguishable by one boolean rather than needing a whole
separate parser.
"""
from __future__ import annotations

import json
import re

# An Ed25519 public key is 32 bytes -- exactly 64 lowercase hex characters.
# The pubkey from a hello ends up as a directory name under peer_feeds
# (net.py's mirror_dir), so a value carrying "/" or ".." would be a path
# traversal. Today the *write* path keys off the cryptographic
# conn.remote_id() rather than this field, and iroh's id cannot contain
# those characters -- but this datagram is unauthenticated attacker input,
# and "the caller happens to use a safer value" is not something to rely on
# one refactor from now. Rejecting a malformed pubkey here costs nothing and
# closes the class outright. Found by an adversarial pass: decode_hello
# previously accepted "../../../../tmp/x" as a pubkey.
_PUBKEY_RE = re.compile(r"\A[0-9a-f]{64}\Z")

# `code` and `hostname` are display-only -- shown to a human deciding which
# beacon is really their other device (device_sync.pair_over_lan) -- never a
# path, a key, or anything else load-bearing, so their guard is just a
# generous length cap against a hostile giant string, not a strict charset.
_MAX_CODE_LEN = 16
_MAX_HOSTNAME_LEN = 128

# `known_peers`: a small, bounded gossip payload -- "here are a few other
# roastmesh nodes I've recently heard from" -- added 2026-09-07 after live
# testing showed a fixed third-party rendezvous host (moduloinfo.ca) was
# USELESS for its actual purpose: it can only ever prove "I can reach this
# one specific host," never introduce two strangers to each other, because a
# plain (pubkey, ticket) hello carries no information about anyone else.
# Confirmed directly on real hardware (desktop, Pi, a genuinely external
# VPS): with the old wire format, both sides discovered the third party and
# NEVER each other, no matter how long they waited.
#
# Capped at _MAX_KNOWN_PEERS entries to keep the datagram comfortably under
# any real-world path MTU -- a real ticket runs roughly 180-220 chars, so 4
# of them plus pubkeys and JSON overhead stays well under 1200 bytes, a
# generally-safe unfragmented UDP payload size. This is deliberately NOT
# rendezvous-host-specific: ANY node populates it from whoever it has
# recently heard from, so introduction propagates as ordinary gossip through
# the whole mesh rather than depending on one always-on host being up --
# see run_wan_discovery's own recent-peers cache for how it's populated.
_MAX_KNOWN_PEERS = 4
_MAX_TICKET_LEN = 512  # generous headroom over a real ticket's ~180-220 chars


def encode_hello(
    pubkey_hex: str, ticket: str, *, pairing: bool = False,
    code: str | None = None, hostname: str | None = None,
    known_peers: list[tuple[str, str]] | None = None,
) -> bytes:
    """The always-on discovery beacon calls this with just (pubkey, ticket),
    and that call's output must stay byte-for-byte what it always was --
    every already-deployed node's decode_hello (this version's and any
    older one's) already parses that exact shape, and there is no reason to
    disturb it. `pairing`/`code`/`hostname` and `known_peers` are additive
    and only ever appear when the caller actually asks for them, which is
    also why `v` only bumps when one of them is used: a plain discovery
    hello has no new fields to be versioned for.
    """
    msg: dict = {"v": 1, "pubkey": pubkey_hex, "ticket": ticket}
    if pairing:
        msg["v"] = 2
        msg["pairing"] = True
        if code is not None:
            msg["code"] = code
        if hostname is not None:
            msg["hostname"] = hostname
    if known_peers:
        msg["v"] = 3
        msg["known_peers"] = [[pk, tk] for pk, tk in known_peers[:_MAX_KNOWN_PEERS]]
    return json.dumps(msg).encode("utf-8")


def decode_hello(
    data: bytes,
) -> tuple[str, str, bool, str | None, str | None, list[tuple[str, str]]] | None:
    """Returns (pubkey, ticket, pairing, code, hostname, known_peers), or
    None if `data` doesn't parse as a well-formed hello at all. `pairing`
    defaults to False, `code`/`hostname` to None, and `known_peers` to an
    empty list for a hello with none of those keys -- including one from an
    older build that has never heard of them -- so every existing caller
    that only ever cared about (pubkey, ticket) keeps working unchanged; the
    discovery loops that don't use `known_peers` just unpack and ignore it.

    Each known_peers entry is independently validated (a well-formed pubkey,
    a non-empty ticket under _MAX_TICKET_LEN) and a malformed entry is
    dropped rather than rejecting the whole hello -- this is unauthenticated
    input from a peer relaying THIRD-PARTY data it received itself, so a
    single bad entry (buggy or hostile) must not take down an otherwise
    valid introduction to everyone else in the same list.
    """
    try:
        msg = json.loads(data.decode("utf-8"))
        pubkey = msg["pubkey"]
        ticket = msg["ticket"]
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError, TypeError):
        return None
    if not isinstance(pubkey, str) or not isinstance(ticket, str):
        return None
    if not _PUBKEY_RE.match(pubkey):
        return None

    pairing = bool(msg.get("pairing", False))

    code = msg.get("code")
    if not isinstance(code, str) or not code or len(code) > _MAX_CODE_LEN:
        code = None

    hostname = msg.get("hostname")
    if not isinstance(hostname, str) or not hostname or len(hostname) > _MAX_HOSTNAME_LEN:
        hostname = None

    known_peers: list[tuple[str, str]] = []
    raw_known_peers = msg.get("known_peers")
    if isinstance(raw_known_peers, list):
        for entry in raw_known_peers[:_MAX_KNOWN_PEERS]:
            if (isinstance(entry, list) and len(entry) == 2
                    and isinstance(entry[0], str) and isinstance(entry[1], str)
                    and _PUBKEY_RE.match(entry[0])
                    and 0 < len(entry[1]) <= _MAX_TICKET_LEN):
                known_peers.append((entry[0], entry[1]))

    return pubkey, ticket, pairing, code, hostname, known_peers
