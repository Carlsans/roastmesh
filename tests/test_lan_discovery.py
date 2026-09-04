import asyncio
import socket

import sys

import pytest

from roastmesh import lan_discovery
from roastmesh.interfaces import Interface
from roastmesh.lan_discovery import PairingCandidate, discover_pairing_beacons, probe_reachable_devices, run_beacon

# a dedicated test port so this never collides with a real roastmesh node
# (or another test run) using the production default
TEST_PORT = 41999


# Two beacons on one host is a Linux/macOS-only arrangement. There, a
# 255.255.255.255 broadcast loops back to every other socket bound to that
# port on the same machine, which is what lets these tests run both sides in
# one process. Windows does not loop limited broadcasts back to other local
# sockets, so the beacons never see each other and the exchange these tests
# assert cannot happen there.
#
# Skipped rather than reworked, and the gap stated plainly: this means LAN
# discovery *between two Windows machines* is *unverified*. It is not known to
# be broken -- the broadcast does leave the host, and the wire format is
# platform-independent -- but nothing here proves it works, and the honest
# place to prove it is two real machines, not a rewritten fixture.
_needs_broadcast_loopback = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows does not loop limited broadcasts back to other sockets on the same host",
)


@_needs_broadcast_loopback
async def test_two_beacons_discover_each_other() -> None:
    discovered_by_a: list[tuple[str, str]] = []
    discovered_by_b: list[tuple[str, str]] = []

    async def on_a_discovers(pubkey: str, ticket: str) -> None:
        discovered_by_a.append((pubkey, ticket))

    async def on_b_discovers(pubkey: str, ticket: str) -> None:
        discovered_by_b.append((pubkey, ticket))

    task_a = asyncio.create_task(run_beacon(
        "aa" + "0"*62, "ticket-a", on_a_discovers, port=TEST_PORT, interval_s=0.2,
    ))
    task_b = asyncio.create_task(run_beacon(
        "bb" + "0"*62, "ticket-b", on_b_discovers, port=TEST_PORT, interval_s=0.2,
    ))
    try:
        for _ in range(50):
            await asyncio.sleep(0.1)
            if discovered_by_a and discovered_by_b:
                break

        assert discovered_by_a == [("bb" + "0"*62, "ticket-b")]
        assert discovered_by_b == [("aa" + "0"*62, "ticket-a")]
    finally:
        task_a.cancel()
        task_b.cancel()
        for t in (task_a, task_b):
            try:
                await t
            except asyncio.CancelledError:
                pass


async def test_own_beacon_is_never_reported_as_discovered() -> None:
    discovered: list[tuple[str, str]] = []

    async def on_discover(pubkey: str, ticket: str) -> None:
        discovered.append((pubkey, ticket))

    task = asyncio.create_task(run_beacon(
        "cc" + "0"*62, "solo-ticket", on_discover, port=TEST_PORT + 1, interval_s=0.15,
    ))
    try:
        await asyncio.sleep(1.0)
        assert discovered == []
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_repeated_beacons_are_debounced_within_resync_window() -> None:
    discovered: list[tuple[str, str]] = []

    async def on_discover(pubkey: str, ticket: str) -> None:
        discovered.append((pubkey, ticket))

    task_listener = asyncio.create_task(run_beacon(
        "dd" + "0"*62, "listener-ticket", on_discover,
        port=TEST_PORT + 2, interval_s=999, resync_interval_s=5.0,
    ))
    task_chatty = asyncio.create_task(run_beacon(
        "ee" + "0"*62, "chatty-ticket", lambda p, t: asyncio.sleep(0),
        port=TEST_PORT + 2, interval_s=0.15, resync_interval_s=999,
    ))
    try:
        # several beacon intervals' worth of time, well within the 5s resync window
        await asyncio.sleep(1.5)
        assert discovered == [("ee" + "0"*62, "chatty-ticket")]
    finally:
        task_listener.cancel()
        task_chatty.cancel()
        for t in (task_listener, task_chatty):
            try:
                await t
            except asyncio.CancelledError:
                pass


# --- pairing-mode beacons ---------------------------------------------------

@_needs_broadcast_loopback
async def test_two_pairing_beacons_discover_each_other() -> None:
    """The LAN half of device_sync.pair_over_lan: two devices in pairing
    mode find each other's pubkey/ticket/code/hostname, same broadcast/
    multicast machinery as the always-on beacon, just time-bounded."""
    results = await asyncio.gather(
        discover_pairing_beacons(
            "aa" + "0" * 62, "ticket-a", code="1111", hostname="host-a",
            port=TEST_PORT + 10, interval_s=0.1, listen_s=1.0,
        ),
        discover_pairing_beacons(
            "bb" + "0" * 62, "ticket-b", code="2222", hostname="host-b",
            port=TEST_PORT + 10, interval_s=0.1, listen_s=1.0,
        ),
    )
    candidates_a, candidates_b = results
    assert candidates_a == [PairingCandidate("bb" + "0" * 62, "ticket-b", "2222", "host-b")]
    assert candidates_b == [PairingCandidate("aa" + "0" * 62, "ticket-a", "1111", "host-a")]


async def test_pairing_beacon_ignores_a_plain_non_pairing_beacon() -> None:
    """A regular (non-pairing) discovery beacon running on the same LAN at
    the same time must never show up as a pairing candidate -- pairing mode
    only ever wants to find another device that is ALSO actively pairing
    right now."""
    port = TEST_PORT + 11
    plain_task = asyncio.create_task(run_beacon(
        "cc" + "0" * 62, "ticket-c", lambda p, t: asyncio.sleep(0), port=port, interval_s=0.1,
    ))
    try:
        candidates = await discover_pairing_beacons(
            "aa" + "0" * 62, "ticket-a", code="1111", hostname="host-a",
            port=port, interval_s=0.1, listen_s=0.6,
        )
        assert candidates == []
    finally:
        plain_task.cancel()
        try:
            await plain_task
        except asyncio.CancelledError:
            pass


async def test_pairing_beacon_never_reports_its_own_broadcast() -> None:
    candidates = await discover_pairing_beacons(
        "dd" + "0" * 62, "ticket-d", code="9999", hostname="solo",
        port=TEST_PORT + 12, interval_s=0.1, listen_s=0.4,
    )
    assert candidates == []


# --- probe_reachable_devices ------------------------------------------------

@_needs_broadcast_loopback
async def test_probe_reachable_devices_hears_a_regular_beacon() -> None:
    port = TEST_PORT + 20
    beacon_task = asyncio.create_task(run_beacon(
        "ff" + "0" * 62, "ticket-f", lambda p, t: asyncio.sleep(0), port=port, interval_s=0.1,
    ))
    try:
        seen = await probe_reachable_devices(port=port, duration_s=0.5)
        assert seen == {"ff" + "0" * 62: "ticket-f"}
    finally:
        beacon_task.cancel()
        try:
            await beacon_task
        except asyncio.CancelledError:
            pass


async def test_probe_reachable_devices_ignores_a_pairing_mode_beacon() -> None:
    port = TEST_PORT + 21
    pairing_task = asyncio.create_task(discover_pairing_beacons(
        "11" + "0" * 62, "ticket-pairing", code="1234", hostname="pairing-host",
        port=port, interval_s=0.1, listen_s=1.0,
    ))
    try:
        seen = await probe_reachable_devices(port=port, duration_s=0.5)
        assert seen == {}
    finally:
        pairing_task.cancel()
        try:
            await pairing_task
        except asyncio.CancelledError:
            pass


async def test_probe_reachable_devices_finds_nothing_when_nobody_is_beaconing() -> None:
    seen = await probe_reachable_devices(port=TEST_PORT + 22, duration_s=0.3)
    assert seen == {}


# --- announcing on every interface, not just the default route's -----------

class _FakeSock:
    def __init__(self) -> None:
        self.multicast_if: list[str] = []

    def setsockopt(self, level, opt, value):
        if opt == socket.IP_MULTICAST_IF:
            self.multicast_if.append(socket.inet_ntoa(value))


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int]] = []

    def sendto(self, _payload, addr):
        self.sent.append(addr)


def test_the_beacon_goes_out_every_interface_not_just_the_routed_one(monkeypatch) -> None:
    """The measured bug, in miniature.

    On a Raspberry Pi with a VPN up, `ip route get 255.255.255.255` resolves to
    the tunnel, so the single global broadcast the beacon used to send went down
    the VPN and the machine's real LAN on wlan0 never heard it -- LAN discovery
    found nobody on the one network where it should be effortless. The fix is to
    stop asking the routing table and address each interface directly.
    """
    interfaces = [
        Interface("wlan0", 3, "192.168.2.19", "192.168.2.255"),   # the LAN
        Interface("tun0", 9, "10.137.8.74", None),                # the VPN
    ]
    monkeypatch.setattr(lan_discovery, "local_interfaces", lambda: interfaces)
    sock, transport = _FakeSock(), _FakeTransport()

    lan_discovery._announce(sock, transport, b"hello", 41888)

    assert ("192.168.2.255", 41888) in transport.sent, "the real LAN was not addressed"
    # The tunnel has no broadcast address, so it gets the multicast announce
    # only -- and both interfaces get one, each scoped to itself.
    assert sock.multicast_if == ["192.168.2.19", "10.137.8.74"]
    assert transport.sent.count((lan_discovery.MULTICAST_GROUP, 41888)) == 2
    # And nothing goes to the global broadcast any more: on that Pi it reached
    # only the VPN's far end, handing it our pubkey and ticket for nothing.
    assert ("255.255.255.255", 41888) not in transport.sent


def test_an_unknown_platform_keeps_the_old_global_broadcast(monkeypatch) -> None:
    """Enumeration is best-effort and platform-specific. Where it returns
    nothing we must behave exactly as before rather than going silent."""
    monkeypatch.setattr(lan_discovery, "local_interfaces", list)
    sock, transport = _FakeSock(), _FakeTransport()

    lan_discovery._announce(sock, transport, b"hello", 41888)

    assert transport.sent == [("255.255.255.255", 41888)]
    assert sock.multicast_if == []


def test_multicast_groups_are_rejoined_as_interfaces_appear(monkeypatch) -> None:
    """A group joined only at startup is never joined on an interface that
    turns up later -- wifi associating after launch, a VPN connecting, a cable
    going in. Joining again is refused by the kernel for groups we already
    hold, so re-running it every announce is the cheapest way to stay current.
    """
    interfaces = [Interface("wlan0", 3, "192.168.2.19", "192.168.2.255", "255.255.255.0")]
    monkeypatch.setattr(lan_discovery, "local_interfaces", lambda: interfaces)
    joined: list[str] = []

    class _Sock:
        def setsockopt(self, _level, opt, value):
            if opt == socket.IP_ADD_MEMBERSHIP:
                joined.append(socket.inet_ntoa(value[4:8]))

    sock, transport = _Sock(), _FakeTransport()
    lan_discovery._announce(sock, transport, b"hello", 41888)
    assert joined == ["192.168.2.19"]

    # A second interface appears after the beacon was already running.
    interfaces.append(Interface("tun0", 9, "10.137.8.74", None, "255.255.0.0"))
    lan_discovery._announce(sock, transport, b"hello", 41888)
    assert "10.137.8.74" in joined, "an interface that appeared later was never joined"
