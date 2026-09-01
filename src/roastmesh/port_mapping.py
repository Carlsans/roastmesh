"""Ask the router to forward a port, so the user doesn't have to.

`--public-port` works but requires knowing a port is forwarded and typing it
in. Most home routers will hand one out for the asking, over one of two small
UDP protocols on port 5351: PCP (RFC 6887, the modern one) and NAT-PMP
(RFC 6886, its predecessor). Transmission vendors `libnatpmp` for exactly this;
the protocol is small enough to speak directly, unlike UPnP IGD, whose library
is nearly four times the size and mostly router-quirk workarounds.

**Nothing here is believed on its own.** A mapping request that returns success
is a claim by a device with every incentive to be optimistic, and the usual
UPnP experience is a library reporting a mapping while nothing can reach you.
The caller announces the port it was given and then confirms with the existing
read-back check -- if a fresh lookup cannot find that address, the mapping did
not work, whatever the router said.

This cannot help behind carrier-grade NAT: there is no router of yours to ask.
A VPN's forwarded port, entered manually, remains the way out of that.
"""
from __future__ import annotations

import asyncio
import os
import socket
import struct
from dataclasses import dataclass

from roastmesh.interfaces import default_gateway

PORT_MAPPING_PORT = 5351
DEFAULT_LIFETIME_S = 3600

# PCP wants a 96-bit nonce to match its response to our request; it is not a
# security boundary, just a correlation id.
_PCP_NONCE_BYTES = 12
_PROTO_UDP = 17


@dataclass(frozen=True)
class Mapping:
    external_port: int
    lifetime_s: int
    protocol: str  # "pcp" or "natpmp" -- which one answered


def _local_address_towards(gateway: str, port: int = PORT_MAPPING_PORT) -> str | None:
    """Which of our addresses the router will see us as. PCP requires it in the
    request, and it must be the one on the path to the gateway rather than
    whichever address happens to be first."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((gateway, port))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _exchange(gateway: str, payload: bytes, *, timeout: float,
              port: int = PORT_MAPPING_PORT) -> bytes | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(payload, (gateway, port))
        data, _ = sock.recvfrom(1100)
        return data
    except OSError:
        # No router at all, nothing listening on 5351, or ICMP port-unreachable
        # -- all "this router does not do this", none of them worth an error.
        return None
    finally:
        sock.close()


def _pcp_request(internal_port: int, client_ip: str, lifetime_s: int, nonce: bytes) -> bytes:
    # RFC 6887 s11.1: 24-byte header, then the MAP opcode's 36 bytes.
    client = b"\x00" * 10 + b"\xff\xff" + socket.inet_aton(client_ip)  # v4-mapped v6
    header = struct.pack(">BBHI", 2, 1, 0, lifetime_s) + client
    body = (nonce + struct.pack(">B3x", _PROTO_UDP)
            + struct.pack(">HH", internal_port, internal_port) + b"\x00" * 16)
    return header + body


def _parse_pcp(data: bytes, nonce: bytes) -> Mapping | None:
    if len(data) < 60 or data[0] != 2 or data[1] != 0x81:
        return None
    result = data[3]
    if result != 0:  # 0 == SUCCESS; anything else is a refusal with a reason
        return None
    lifetime = struct.unpack(">I", data[4:8])[0]
    if data[24:36] != nonce:
        return None  # a reply to somebody else's request
    external_port = struct.unpack(">H", data[42:44])[0]
    return Mapping(external_port=external_port, lifetime_s=lifetime, protocol="pcp")


def _natpmp_request(internal_port: int, lifetime_s: int) -> bytes:
    # RFC 6886 s3.3: version 0, opcode 1 (UDP), reserved, ports, lifetime.
    return struct.pack(">BBHHHI", 0, 1, 0, internal_port, internal_port, lifetime_s)


def _parse_natpmp(data: bytes) -> Mapping | None:
    if len(data) < 16 or data[0] != 0 or data[1] != 129:
        return None
    result = struct.unpack(">H", data[2:4])[0]
    if result != 0:
        return None
    external_port, lifetime = struct.unpack(">HI", data[10:16])
    return Mapping(external_port=external_port, lifetime_s=lifetime, protocol="natpmp")


def _map_blocking(internal_port: int, gateway: str, lifetime_s: int, timeout: float,
                  port: int) -> Mapping | None:
    client_ip = _local_address_towards(gateway, port)
    if client_ip is not None:
        nonce = os.urandom(_PCP_NONCE_BYTES)
        reply = _exchange(gateway, _pcp_request(internal_port, client_ip, lifetime_s, nonce),
                          timeout=timeout, port=port)
        if reply is not None:
            mapping = _parse_pcp(reply, nonce)
            if mapping is not None:
                return mapping
    # PCP first, NAT-PMP second: they share a port, and a PCP-only router
    # answers a NAT-PMP request with an UNSUPP_VERSION error rather than a
    # mapping, so trying the older one first would work but waste a round trip
    # on every modern router.
    reply = _exchange(gateway, _natpmp_request(internal_port, lifetime_s), timeout=timeout,
                      port=port)
    return _parse_natpmp(reply) if reply is not None else None


async def map_udp_port(internal_port: int, *, gateway: str | None = None,
                       lifetime_s: int = DEFAULT_LIFETIME_S,
                       timeout: float = 2.0,
                       port: int = PORT_MAPPING_PORT) -> Mapping | None:
    """Try to have `internal_port` forwarded to this machine.

    Returns the mapping the router claims to have made, or None. A returned
    Mapping is a claim, not a fact -- verify it before telling anyone about it.
    """
    gateway = gateway or default_gateway()
    if gateway is None:
        return None
    return await asyncio.to_thread(_map_blocking, internal_port, gateway, lifetime_s,
                                   timeout, port)
