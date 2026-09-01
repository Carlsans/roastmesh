"""interfaces.py.

The platform-specific halves can only run where they run: the Linux ioctls are
exercised on Linux, the Windows GetAdaptersInfo binding on Windows CI. What is
*not* platform-specific is the arithmetic that turns an address and a netmask
into a broadcast address, and that is where a mistake would actually live --
Windows would then quietly announce to the wrong address with no error to show
for it. So that part is pulled out and tested everywhere.
"""
from __future__ import annotations

import socket
import sys

import pytest

from roastmesh.interfaces import Interface, broadcast_for, default_gateway, local_interfaces


@pytest.mark.parametrize(("address", "netmask", "expected"), [
    ("192.168.0.222", "255.255.255.0", "192.168.0.255"),
    ("192.168.2.19", "255.255.255.0", "192.168.2.255"),
    ("10.137.8.74", "255.255.0.0", "10.137.255.255"),
    ("172.17.0.1", "255.255.0.0", "172.17.255.255"),
    ("203.0.113.5", "255.255.255.240", "203.0.113.15"),
    ("100.123.46.13", "255.255.255.255", None),   # a /32 has no broadcast
    ("10.0.0.1", "0.0.0.0", None),                # no subnet at all
    ("not-an-ip", "255.255.255.0", None),
    ("10.0.0.1", "garbage", None),
])
def test_broadcast_for(address: str, netmask: str, expected: str | None) -> None:
    assert broadcast_for(address, netmask) == expected


def test_every_interface_reports_a_usable_address() -> None:
    """Whatever the platform returns has to be something we can actually send
    to -- a malformed entry here becomes a silent OSError inside the beacon,
    which is exactly the kind of quiet nothing this module exists to end."""
    for iface in local_interfaces():
        assert isinstance(iface, Interface)
        socket.inet_aton(iface.address)          # raises if it is not a dotted quad
        assert not iface.address.startswith("127."), "loopback should be filtered out"
        if iface.broadcast is not None:
            socket.inet_aton(iface.broadcast)
            assert iface.broadcast != iface.address


def test_the_gateway_is_an_address_or_nothing() -> None:
    gw = default_gateway()
    if gw is not None:
        socket.inet_aton(gw)
        assert gw != "0.0.0.0"


@pytest.mark.skipif(sys.platform != "win32", reason="exercises the GetAdaptersInfo binding")
def test_windows_enumeration_actually_returns_adapters() -> None:
    """The ctypes struct layout cannot be checked by reading it -- a field in
    the wrong place produces plausible-looking rubbish rather than an error.
    A CI runner always has at least one configured adapter, so an empty list
    here means the binding is wrong.
    """
    interfaces = local_interfaces()
    assert interfaces, "GetAdaptersInfo returned nothing on a machine that has adapters"
    for iface in interfaces:
        socket.inet_aton(iface.address)
        assert iface.index >= 0
        assert iface.name


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/net/route")
def test_linux_enumeration_finds_the_machines_own_lan() -> None:
    addresses = {i.address for i in local_interfaces()}
    assert addresses, "no interfaces found on a machine that certainly has one"
