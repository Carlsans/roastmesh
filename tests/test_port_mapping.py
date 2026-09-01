"""port_mapping.py against a fake router.

The real thing was confirmed working against the development machine's own
router (which answered NAT-PMP with a 3600-second lease), but a test cannot
depend on whatever hardware happens to be on the other end of the cable. These
drive a router we write ourselves, in-process, the same way test_wan_discovery
drives a fake DHT.
"""
from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from roastmesh.port_mapping import (
    Mapping,
    _natpmp_request,
    _parse_natpmp,
    _parse_pcp,
    _pcp_request,
    default_gateway,
    map_udp_port,
)


class _FakeRouter(asyncio.DatagramProtocol):
    """Answers one or both of the two protocols, or neither.

    `speaks` picks which: routers in the wild do all three, and which one
    answers changes what we should send next.
    """

    def __init__(self, *, speaks: str, external_port: int = 26513,
                 lifetime: int = 3600, result: int = 0) -> None:
        self.speaks = speaks
        self.external_port = external_port
        self.lifetime = lifetime
        self.result = result
        self.seen: list[str] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if data[:1] == b"\x02":                      # PCP
            self.seen.append("pcp")
            if self.speaks not in ("pcp", "both"):
                return
            nonce = data[24:36]
            reply = (struct.pack(">BBBB", 2, 0x81, 0, self.result)
                     + struct.pack(">II", self.lifetime, 0) + b"\x00" * 12
                     + nonce + struct.pack(">B3x", 17)
                     + struct.pack(">HH", 41890, self.external_port) + b"\x00" * 16)
            self.transport.sendto(reply, addr)
        elif data[:1] == b"\x00":                    # NAT-PMP
            self.seen.append("natpmp")
            if self.speaks not in ("natpmp", "both"):
                return
            reply = struct.pack(">BBHIHHI", 0, 129, self.result, 0,
                                41890, self.external_port, self.lifetime)
            self.transport.sendto(reply, addr)


async def _start_router(**kwargs) -> tuple[asyncio.DatagramTransport, _FakeRouter, int]:
    loop = asyncio.get_running_loop()
    router = _FakeRouter(**kwargs)
    transport, _ = await loop.create_datagram_endpoint(lambda: router, local_addr=("127.0.0.1", 0))
    return transport, router, transport.get_extra_info("sockname")[1]


async def test_a_pcp_router_grants_a_mapping() -> None:
    transport, router, port = await _start_router(speaks="pcp")
    try:
        mapping = await map_udp_port(41890, gateway="127.0.0.1", port=port, timeout=2.0)
        assert mapping == Mapping(external_port=26513, lifetime_s=3600, protocol="pcp")
        assert router.seen == ["pcp"], "NAT-PMP should not be tried once PCP answers"
    finally:
        transport.close()


async def test_a_natpmp_only_router_still_works() -> None:
    """PCP is tried first and ignored by an older router, so the fallback has
    to actually happen rather than the whole attempt timing out."""
    transport, router, port = await _start_router(speaks="natpmp")
    try:
        mapping = await map_udp_port(41890, gateway="127.0.0.1", port=port, timeout=1.0)
        assert mapping == Mapping(external_port=26513, lifetime_s=3600, protocol="natpmp")
        assert router.seen == ["pcp", "natpmp"]
    finally:
        transport.close()


async def test_a_router_that_speaks_neither_is_not_an_error() -> None:
    """Most of the point: no mapping is an ordinary outcome that leaves the
    user exactly where they were, not a failure to report."""
    transport, _router, port = await _start_router(speaks="none")
    try:
        assert await map_udp_port(41890, gateway="127.0.0.1", port=port, timeout=0.4) is None
    finally:
        transport.close()


async def test_a_refusal_is_not_read_as_a_mapping() -> None:
    """A non-zero result code with an otherwise well-formed reply. Reading the
    port out of it anyway would have us announce a port the router explicitly
    declined to open."""
    transport, _router, port = await _start_router(speaks="both", result=2)  # NOT_AUTHORIZED
    try:
        assert await map_udp_port(41890, gateway="127.0.0.1", port=port, timeout=0.6) is None
    finally:
        transport.close()


def test_a_pcp_reply_to_someone_elses_request_is_ignored() -> None:
    """The nonce is a correlation id, and honouring a reply carrying the wrong
    one would let any host on the LAN hand us a port number of its choosing."""
    ours, theirs = b"a" * 12, b"b" * 12
    reply = (struct.pack(">BBBB", 2, 0x81, 0, 0) + struct.pack(">II", 3600, 0) + b"\x00" * 12
             + theirs + struct.pack(">B3x", 17) + struct.pack(">HH", 41890, 9999) + b"\x00" * 16)
    assert _parse_pcp(reply, ours) is None
    assert _parse_pcp(reply, theirs) is not None


def test_malformed_replies_are_ignored_rather_than_unpacked() -> None:
    for junk in (b"", b"\x02", b"\x00" * 8, b"\xff" * 64):
        assert _parse_pcp(junk, b"n" * 12) is None
        assert _parse_natpmp(junk) is None


def test_the_requests_match_the_rfc_layouts() -> None:
    """Pinned by length and header bytes: a request one field out is accepted
    by nobody and produces the same silence as having no router at all, which
    is indistinguishable from working correctly on a network without one."""
    pcp = _pcp_request(41890, "192.168.0.222", 3600, b"n" * 12)
    assert len(pcp) == 60                       # RFC 6887: 24-byte header + 36-byte MAP
    assert pcp[0] == 2 and pcp[1] == 1          # version 2, MAP request
    assert pcp[24:36] == b"n" * 12

    natpmp = _natpmp_request(41890, 3600)
    assert len(natpmp) == 12                    # RFC 6886 s3.3
    assert natpmp[0] == 0 and natpmp[1] == 1    # version 0, map UDP
    assert struct.unpack(">H", natpmp[4:6])[0] == 41890


def test_gateway_discovery_returns_an_address_or_nothing() -> None:
    gw = default_gateway()
    if gw is not None:
        socket.inet_aton(gw)  # raises if it is not a dotted quad
