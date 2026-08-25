"""The one-datagram "here's my feed and ticket" announcement, shared by
lan_discovery (broadcast) and wan_discovery (DHT-rendezvous'd unicast) --
same wire format either way, so a node discovered over the internet is
handled by exactly the same code path as one discovered on the LAN.
"""
from __future__ import annotations

import json


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
    return pubkey, ticket
