import asyncio

import sys

import pytest

from roastmesh.lan_discovery import run_beacon

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
        "pubkey-a", "ticket-a", on_a_discovers, port=TEST_PORT, interval_s=0.2,
    ))
    task_b = asyncio.create_task(run_beacon(
        "pubkey-b", "ticket-b", on_b_discovers, port=TEST_PORT, interval_s=0.2,
    ))
    try:
        for _ in range(50):
            await asyncio.sleep(0.1)
            if discovered_by_a and discovered_by_b:
                break

        assert discovered_by_a == [("pubkey-b", "ticket-b")]
        assert discovered_by_b == [("pubkey-a", "ticket-a")]
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
        "solo-pubkey", "solo-ticket", on_discover, port=TEST_PORT + 1, interval_s=0.15,
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
        "listener", "listener-ticket", on_discover,
        port=TEST_PORT + 2, interval_s=999, resync_interval_s=5.0,
    ))
    task_chatty = asyncio.create_task(run_beacon(
        "chatty", "chatty-ticket", lambda p, t: asyncio.sleep(0),
        port=TEST_PORT + 2, interval_s=0.15, resync_interval_s=999,
    ))
    try:
        # several beacon intervals' worth of time, well within the 5s resync window
        await asyncio.sleep(1.5)
        assert discovered == [("chatty", "chatty-ticket")]
    finally:
        task_listener.cancel()
        task_chatty.cancel()
        for t in (task_listener, task_chatty):
            try:
                await t
            except asyncio.CancelledError:
                pass
