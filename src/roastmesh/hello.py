"""The one-datagram "here's my feed and ticket" announcement, shared by
lan_discovery (broadcast) and wan_discovery (DHT-rendezvous'd unicast) --
same wire format either way, so a node discovered over the internet is
handled by exactly the same code path as one discovered on the LAN.
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


def encode_hello(pubkey_hex: str, ticket: str) -> bytes:
    return json.dumps({"v": 1, "pubkey": pubkey_hex, "ticket": ticket}).encode("utf-8")


def decode_hello(data: bytes) -> tuple[str, str] | None:
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
    return pubkey, ticket
