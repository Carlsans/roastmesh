"""Which network interfaces this machine actually has, and how to reach the
LAN on each of them.

Existing solely because of a measured bug: `lan_discovery` sent one datagram
to 255.255.255.255 and let the routing table choose an interface. On a machine
with a VPN up, the routing table chooses the tunnel:

    pi$ ip route get 255.255.255.255
    broadcast 255.255.255.255 dev tun0 src 10.137.8.74   <- the VPN
    pi$ ip -brief addr
    wlan0   UP   192.168.2.19/24                         <- the actual LAN

so the beacon went down the tunnel and no machine on the Pi's own network ever
heard it. Announcing per interface instead of once into whichever one the
default route names is the fix, and that needs a list of interfaces.

There is no dependency-free, cross-platform way to get one. Each platform gets
what it can, and **an unrecognised platform returns nothing**, which the caller
treats as "fall back to the single global broadcast" -- i.e. exactly today's
behaviour, so nothing regresses anywhere.
"""
from __future__ import annotations

import socket
import struct
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Interface:
    """One usable IPv4 interface.

    `broadcast` is None when the platform can give us an address but not a
    netmask (Windows, below). That is not fatal -- the multicast announce only
    needs `address`, and the caller still sends one global broadcast as well.
    """

    name: str
    index: int
    address: str
    broadcast: str | None


# Linux ioctls. Values from <bits/ioctls.h>; stable ABI, not worth a lookup.
_SIOCGIFFLAGS = 0x8913
_SIOCGIFADDR = 0x8915
_SIOCGIFBRDADDR = 0x8919

_IFF_UP = 0x1
_IFF_BROADCAST = 0x2
_IFF_LOOPBACK = 0x8


def _linux_ioctl(sock, request: int, name: str) -> bytes | None:
    import fcntl

    try:
        return fcntl.ioctl(sock.fileno(), request, struct.pack("256s", name.encode()[:15]))
    except OSError:
        # An interface can disappear between being listed and being asked
        # about (a VPN dropping, a cable pulled). Skip it rather than failing
        # the whole enumeration for one entry.
        return None


def _linux_interfaces() -> list[Interface]:
    import fcntl  # noqa: F401 -- presence check; _linux_ioctl does the work

    out: list[Interface] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for index, name in socket.if_nameindex():
            flags_raw = _linux_ioctl(sock, _SIOCGIFFLAGS, name)
            if flags_raw is None:
                continue
            flags = struct.unpack("H", flags_raw[16:18])[0]
            if not flags & _IFF_UP or flags & _IFF_LOOPBACK:
                continue

            addr_raw = _linux_ioctl(sock, _SIOCGIFADDR, name)
            if addr_raw is None:
                continue  # no IPv4 on this interface (v6-only, or not configured)
            address = socket.inet_ntoa(addr_raw[20:24])

            broadcast = None
            if flags & _IFF_BROADCAST:
                brd_raw = _linux_ioctl(sock, _SIOCGIFBRDADDR, name)
                if brd_raw is not None:
                    candidate = socket.inet_ntoa(brd_raw[20:24])
                    # A point-to-point link (a VPN tunnel) reports the *peer*
                    # address here rather than a broadcast address, and some
                    # report 0.0.0.0. Neither is something to send a LAN
                    # broadcast to.
                    if candidate not in ("0.0.0.0", address):
                        broadcast = candidate
            out.append(Interface(name=name, index=index, address=address, broadcast=broadcast))
    finally:
        sock.close()
    return out


def _windows_interfaces() -> list[Interface]:
    """Addresses only, via the machine's own name.

    Deliberately not GetAdaptersAddresses through ctypes: this needs only the
    per-interface *address* (which is what Windows' IP_MULTICAST_IF wants
    anyway), and resolving our own hostname yields those without several
    hundred lines of struct declarations to get wrong. The cost is no netmask,
    so no directed broadcast -- hence `broadcast=None`.
    """
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET,
                                   type=socket.SOCK_DGRAM)
    except OSError:
        return []
    out: list[Interface] = []
    for i, info in enumerate(dict.fromkeys(a[4][0] for a in infos)):
        if info.startswith("127."):
            continue
        out.append(Interface(name=f"if{i}", index=0, address=info, broadcast=None))
    return out


def local_interfaces() -> list[Interface]:
    """Every usable, non-loopback IPv4 interface, or [] if we cannot tell.

    An empty list is a legitimate answer, not an error: the caller keeps the
    single global broadcast it has always sent, so an unsupported platform is
    no worse off than before this module existed.
    """
    try:
        if sys.platform.startswith("linux"):
            return _linux_interfaces()
        if sys.platform == "win32":
            return _windows_interfaces()
    except Exception:  # noqa: BLE001 -- enumeration must never break discovery
        return []
    return []
