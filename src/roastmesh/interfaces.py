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

    `broadcast` is None where the subnet has no meaningful broadcast address:
    a /32, or a point-to-point link like a VPN tunnel, which reports its peer
    rather than a broadcast. Those interfaces still get the multicast announce,
    which needs only `address`.
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


def broadcast_for(address: str, netmask: str) -> str | None:
    """The directed broadcast address of the subnet `address` sits in.

    Pure arithmetic, and separated out precisely so it can be tested on any
    platform: the Windows code path that feeds it can only run on Windows, but
    getting `ip | ~mask` wrong would be a silent bug there, and this is the
    half where the mistake would actually live.
    """
    try:
        ip = struct.unpack(">I", socket.inet_aton(address))[0]
        mask = struct.unpack(">I", socket.inet_aton(netmask))[0]
    except (OSError, struct.error):
        return None
    if mask == 0:
        return None            # no subnet, so no broadcast to speak of
    broadcast = (ip | (~mask & 0xFFFFFFFF)) & 0xFFFFFFFF
    if broadcast == ip:
        return None            # a /32: a point-to-point link or a VPN endpoint
    return socket.inet_ntoa(struct.pack(">I", broadcast))


_MIB_IF_TYPE_LOOPBACK = 24
_ERROR_SUCCESS = 0
_ERROR_BUFFER_OVERFLOW = 111


def _windows_adapters():
    """Walk GetAdaptersInfo, yielding (name, index, ip, netmask, gateway).

    The older IPv4-only API on purpose. GetAdaptersAddresses supersedes it, but
    that means declaring a struct with a dozen pointers into sockaddr unions,
    all of which have to be laid out exactly right or the results are silently
    wrong rather than an error. GetAdaptersInfo hands back plain strings and
    the gateway in the same call, which is everything this module and
    port_mapping need between them.
    """
    import ctypes

    class _IpAddrString(ctypes.Structure):
        pass

    _IpAddrString._fields_ = [
        ("Next", ctypes.POINTER(_IpAddrString)),
        ("IpAddress", ctypes.c_char * 16),
        ("IpMask", ctypes.c_char * 16),
        ("Context", ctypes.c_ulong),
    ]

    class _AdapterInfo(ctypes.Structure):
        pass

    _AdapterInfo._fields_ = [
        ("Next", ctypes.POINTER(_AdapterInfo)),
        ("ComboIndex", ctypes.c_ulong),
        ("AdapterName", ctypes.c_char * 260),
        ("Description", ctypes.c_char * 132),
        ("AddressLength", ctypes.c_uint),
        ("Address", ctypes.c_ubyte * 8),
        ("Index", ctypes.c_ulong),
        ("Type", ctypes.c_uint),
        ("DhcpEnabled", ctypes.c_uint),
        ("CurrentIpAddress", ctypes.POINTER(_IpAddrString)),
        ("IpAddressList", _IpAddrString),
        ("GatewayList", _IpAddrString),
        ("DhcpServer", _IpAddrString),
        ("HaveWins", ctypes.c_int),
        ("PrimaryWinsServer", _IpAddrString),
        ("SecondaryWinsServer", _IpAddrString),
        ("LeaseObtained", ctypes.c_int64),
        ("LeaseExpires", ctypes.c_int64),
    ]

    iphlpapi = ctypes.windll.iphlpapi
    size = ctypes.c_ulong(0)
    # First call sizes the buffer; it is *expected* to fail with
    # ERROR_BUFFER_OVERFLOW, which is how the size comes back.
    rc = iphlpapi.GetAdaptersInfo(None, ctypes.byref(size))
    if rc not in (_ERROR_SUCCESS, _ERROR_BUFFER_OVERFLOW) or size.value == 0:
        return
    buffer = ctypes.create_string_buffer(size.value)
    table = ctypes.cast(buffer, ctypes.POINTER(_AdapterInfo))
    if iphlpapi.GetAdaptersInfo(table, ctypes.byref(size)) != _ERROR_SUCCESS:
        return

    adapter = table
    while adapter:
        entry = adapter.contents
        if entry.Type != _MIB_IF_TYPE_LOOPBACK:
            gateway = entry.GatewayList.IpAddress.decode("ascii", "replace") or None
            if gateway == "0.0.0.0":
                gateway = None
            node = ctypes.pointer(entry.IpAddressList)
            while node:
                ip = node.contents.IpAddress.decode("ascii", "replace")
                mask = node.contents.IpMask.decode("ascii", "replace")
                # An adapter that is present but unconfigured reports 0.0.0.0
                # rather than being absent from the list.
                if ip and ip != "0.0.0.0":
                    yield (entry.AdapterName.decode("ascii", "replace"),
                           int(entry.Index), ip, mask, gateway)
                node = node.contents.Next
        adapter = entry.Next


def _windows_interfaces() -> list[Interface]:
    return [Interface(name=name, index=index, address=ip, broadcast=broadcast_for(ip, mask))
            for name, index, ip, mask, _gateway in _windows_adapters()]


def default_gateway() -> str | None:
    """The default route's next hop, or None if we cannot work it out.

    Used by port_mapping to know who to ask for a forwarded port. Linux reads
    /proc/net/route; Windows takes the first adapter that reports a gateway,
    which is the same thing by another name.
    """
    try:
        if sys.platform.startswith("linux"):
            return _linux_default_gateway()
        if sys.platform == "win32":
            for _name, _index, _ip, _mask, gateway in _windows_adapters():
                if gateway:
                    return gateway
    except Exception:  # noqa: BLE001 -- no gateway is a valid answer, never a crash
        return None
    return None


def _linux_default_gateway() -> str | None:
    with open("/proc/net/route", encoding="ascii") as handle:
        next(handle)  # header
        for line in handle:
            fields = line.split()
            if len(fields) < 3 or fields[1] != "00000000":
                continue
            # Little-endian hex, as the kernel writes it.
            return socket.inet_ntoa(struct.pack("<I", int(fields[2], 16)))
    return None


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
