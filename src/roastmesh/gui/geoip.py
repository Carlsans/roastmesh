"""Offline IPv4 -> ISO-3166 country lookup for the peers list.

A compact, bundled table (gui/data/ip2country.bin.gz, built from DB-IP's
IP-to-Country Lite, CC-BY-4.0) -- no third-party lookup, works offline, and never
sends a peer's address anywhere. The ranges partition the whole IPv4 space
contiguously, so a single `bisect` over the range starts is the whole lookup.

Country is best-effort and only meaningful for a public address: private,
loopback, link-local, and reserved IPs return None (a peer reachable only on the
LAN, or via a relay, has no country to show).
"""
from __future__ import annotations

import bisect
import gzip
import ipaddress
import struct
import sys
from array import array
from importlib import resources

_starts: array | None = None   # sorted uint32 range starts
_ccs: bytes | None = None       # parallel 2-byte country codes


def _load() -> None:
    global _starts, _ccs
    if _starts is not None:
        return
    blob = gzip.decompress(
        resources.files("roastmesh.gui.data").joinpath("ip2country.bin.gz").read_bytes()
    )
    n = struct.unpack_from("<I", blob, 0)[0]
    starts = array("I")
    starts.frombytes(blob[4:4 + 4 * n])
    # The file is little-endian (built on a little-endian host); byteswap on the
    # rare big-endian runtime so the integers read back correctly.
    if sys.byteorder != "little":
        starts.byteswap()
    _starts = starts
    _ccs = blob[4 + 4 * n:4 + 4 * n + 2 * n]


def country_code(ip: str | None) -> str | None:
    """ISO-3166 alpha-2 (upper) for a public IPv4 string, or None (private/
    loopback/reserved/IPv6/unknown/unparseable)."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.version != 4:
        return None
    if (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
        return None
    _load()
    assert _starts is not None and _ccs is not None
    i = bisect.bisect_right(_starts, int(addr)) - 1
    if i < 0:
        return None
    cc = _ccs[2 * i:2 * i + 2].decode("ascii")
    return None if cc == "ZZ" else cc
