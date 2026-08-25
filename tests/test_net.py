import asyncio
import contextlib
import json
from pathlib import Path

import iroh
import pytest

from roastnet import net
from roastnet.feed import append_entry, blob_path_for, read_entries
from roastnet.identity import generate_identity
from roastnet.peers import Peer, load_peers, save_peers
from roastnet.quota import QuotaLimits

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURES_DIR.glob("*.alog"))[:3]


async def _start_server(identity, feed_dir, peers_path):
    ready: asyncio.Future = asyncio.get_event_loop().create_future()
    task = asyncio.create_task(
        net.serve(identity, feed_dir, peers_path, relay=False, ready_callback=ready.set_result)
    )
    ticket = await asyncio.wait_for(ready, timeout=10)
    return task, ticket


async def _stop_server(task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _publish(feed_dir: Path, identity, paths) -> None:
    for i, path in enumerate(paths):
        append_entry(feed_dir, identity, path, timestamp=f"2026-01-{i + 1:02d}T00:00:00Z")


async def test_sync_pulls_all_published_entries(tmp_path: Path) -> None:
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES)
    server_task, ticket = await _start_server(server_identity, server_feed_dir, tmp_path / "server_peers.json")

    try:
        client_identity = generate_identity()
        report = await net.sync_with_peer(
            ticket, client_identity, tmp_path / "client_peer_feeds", tmp_path / "client_peers.json", relay=False,
        )

        assert report.peer_pubkey_hex == server_identity.public_key_hex
        assert report.new_entry_count == len(FIXTURES)
        assert report.verify.ok
        assert report.verify.valid_count == len(FIXTURES)

        mirror_dir = tmp_path / "client_peer_feeds" / server_identity.public_key_hex
        assert len(read_entries(mirror_dir)) == len(FIXTURES)
    finally:
        await _stop_server(server_task)


async def test_second_sync_is_a_noop_when_nothing_new(tmp_path: Path) -> None:
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES)
    server_task, ticket = await _start_server(server_identity, server_feed_dir, tmp_path / "server_peers.json")

    try:
        client_identity = generate_identity()
        peer_feeds_root = tmp_path / "client_peer_feeds"
        peers_path = tmp_path / "client_peers.json"

        first = await net.sync_with_peer(ticket, client_identity, peer_feeds_root, peers_path, relay=False)
        assert first.new_entry_count == len(FIXTURES)

        second = await net.sync_with_peer(ticket, client_identity, peer_feeds_root, peers_path, relay=False)
        assert second.new_entry_count == 0
        assert second.verify.ok
    finally:
        await _stop_server(server_task)


async def test_sync_after_new_publish_pulls_only_the_new_entry(tmp_path: Path) -> None:
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:2])
    server_task, ticket = await _start_server(server_identity, server_feed_dir, tmp_path / "server_peers.json")

    try:
        client_identity = generate_identity()
        peer_feeds_root = tmp_path / "client_peer_feeds"
        peers_path = tmp_path / "client_peers.json"

        first = await net.sync_with_peer(ticket, client_identity, peer_feeds_root, peers_path, relay=False)
        assert first.new_entry_count == 2

        append_entry(server_feed_dir, server_identity, FIXTURES[2], timestamp="2026-02-01T00:00:00Z")

        second = await net.sync_with_peer(ticket, client_identity, peer_feeds_root, peers_path, relay=False)
        assert second.new_entry_count == 1
        assert second.verify.valid_count == 3
    finally:
        await _stop_server(server_task)


async def test_sync_merges_gossiped_peers(tmp_path: Path) -> None:
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:1])
    server_peers_path = tmp_path / "server_peers.json"

    # server B already knows about a third peer C (never actually started --
    # gossip only needs to relay what's *known*, not reach C directly)
    known_peer_c = Peer(
        ticket="deadbeef-fake-ticket-for-peer-c", feed_pubkey_hex="c" * 64,
        first_seen="2026-01-01T00:00:00+00:00", last_seen="2026-01-01T00:00:00+00:00", added_via="manual",
    )
    save_peers([known_peer_c], server_peers_path)

    server_task, ticket = await _start_server(server_identity, server_feed_dir, server_peers_path)
    try:
        client_identity = generate_identity()
        client_peers_path = tmp_path / "client_peers.json"
        await net.sync_with_peer(
            ticket, client_identity, tmp_path / "client_peer_feeds", client_peers_path, relay=False,
        )

        client_peers = load_peers(client_peers_path)
        by_pubkey = {p.feed_pubkey_hex: p for p in client_peers}
        assert "c" * 64 in by_pubkey
        assert by_pubkey["c" * 64].added_via == "gossip"
        # the server itself is also now a known peer, tagged manual (the
        # default added_via for a directly-dialed sync)
        assert server_identity.public_key_hex in by_pubkey
        assert by_pubkey[server_identity.public_key_hex].added_via == "manual"
    finally:
        await _stop_server(server_task)


async def test_sync_rejects_tampered_blob_bytes(tmp_path: Path) -> None:
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:2])

    # corrupt entry 0's blob bytes on the server's own disk, leaving the
    # entry metadata (and its content_sha256) untouched -- this is the
    # realistic version of "a peer serves bad data": the response still
    # builds fine, but the bytes no longer match their claimed hash, and
    # only end-to-end verification on the receiving side can catch it.
    entries = read_entries(server_feed_dir)
    blob_path = blob_path_for(server_feed_dir, entries[0])
    blob_path.write_bytes(b"corrupted, not the original bytes")

    server_task, ticket = await _start_server(server_identity, server_feed_dir, tmp_path / "server_peers.json")
    try:
        client_identity = generate_identity()
        report = await net.sync_with_peer(
            ticket, client_identity, tmp_path / "client_peer_feeds", tmp_path / "client_peers.json", relay=False,
        )
        assert not report.verify.ok
        assert report.verify.valid_count == 0
    finally:
        await _stop_server(server_task)


async def test_sync_raises_when_peer_returns_an_error(tmp_path: Path) -> None:
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:2])

    # point entry 0's content_sha256 at a hash with no corresponding blob
    # file -- the server can't build a get_feed response at all for this,
    # and sync_with_peer must surface that as a failure, not silently
    # report "0 new entries" as if the feed were simply up to date.
    entry_path = server_feed_dir / "entries" / "00000000.json"
    data = json.loads(entry_path.read_text())
    data["content_sha256"] = "0" * 64
    entry_path.write_text(json.dumps(data))

    server_task, ticket = await _start_server(server_identity, server_feed_dir, tmp_path / "server_peers.json")
    try:
        client_identity = generate_identity()
        with pytest.raises(RuntimeError):
            await net.sync_with_peer(
                ticket, client_identity, tmp_path / "client_peer_feeds", tmp_path / "client_peers.json", relay=False,
            )
    finally:
        await _stop_server(server_task)


async def test_get_feed_meta_response_never_includes_blob_bytes(tmp_path: Path) -> None:
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES)
    server_task, ticket = await _start_server(server_identity, server_feed_dir, tmp_path / "server_peers.json")

    try:
        client_identity = generate_identity()
        ep = await net.bind_endpoint(client_identity, relay=False)
        try:
            ticket_obj = iroh.EndpointTicket.from_string(ticket)
            conn = await ep.connect(ticket_obj.endpoint_addr(), net.ALPN)
            response = await net._request(conn, {"op": "get_feed_meta", "since_seq": 0})
            assert len(response["entries"]) == len(FIXTURES)
            assert all("blob_base64" not in e for e in response["entries"])
            assert all("size_bytes" in e for e in response["entries"])
        finally:
            await ep.close()
    finally:
        await _stop_server(server_task)


async def test_sync_respects_max_files_per_feed_quota(tmp_path: Path) -> None:
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES)  # 3 entries
    server_task, ticket = await _start_server(server_identity, server_feed_dir, tmp_path / "server_peers.json")

    try:
        client_identity = generate_identity()
        report = await net.sync_with_peer(
            ticket, client_identity, tmp_path / "client_peer_feeds", tmp_path / "client_peers.json",
            relay=False, limits=QuotaLimits(max_files_per_feed=2),
        )
        assert report.new_entry_count == 2
        assert report.quota.held_back == 1
        assert "max_files_per_feed" in report.quota.reason
        assert report.verify.ok  # the admitted prefix is still fully valid on its own
        assert report.verify.valid_count == 2
    finally:
        await _stop_server(server_task)


async def test_sync_quota_holds_back_oversized_entry(tmp_path: Path) -> None:
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:1])
    # FIXTURES are all well under 1KB-10KB; pick a tiny max_bytes_per_file
    # so the one real entry we published is the "oversized" one.
    server_task, ticket = await _start_server(server_identity, server_feed_dir, tmp_path / "server_peers.json")

    try:
        client_identity = generate_identity()
        report = await net.sync_with_peer(
            ticket, client_identity, tmp_path / "client_peer_feeds", tmp_path / "client_peers.json",
            relay=False, limits=QuotaLimits(max_bytes_per_file=10),
        )
        assert report.new_entry_count == 0
        assert report.quota.held_back == 1
        assert "max_bytes_per_file" in report.quota.reason
    finally:
        await _stop_server(server_task)


async def test_sync_quota_allows_more_once_cap_room_frees_up_across_syncs(tmp_path: Path) -> None:
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES)  # 3 entries
    server_task, ticket = await _start_server(server_identity, server_feed_dir, tmp_path / "server_peers.json")

    try:
        client_identity = generate_identity()
        peer_feeds_root = tmp_path / "client_peer_feeds"
        peers_path = tmp_path / "client_peers.json"
        tight_limits = QuotaLimits(max_files_per_feed=2)

        first = await net.sync_with_peer(
            ticket, client_identity, peer_feeds_root, peers_path, relay=False, limits=tight_limits,
        )
        assert first.new_entry_count == 2

        # same tight cap, nothing more fits -- existing (2) already at the limit
        second = await net.sync_with_peer(
            ticket, client_identity, peer_feeds_root, peers_path, relay=False, limits=tight_limits,
        )
        assert second.new_entry_count == 0
        assert second.quota.held_back == 1

        # raise the cap: the previously held-back entry can now come through
        looser_limits = QuotaLimits(max_files_per_feed=10)
        third = await net.sync_with_peer(
            ticket, client_identity, peer_feeds_root, peers_path, relay=False, limits=looser_limits,
        )
        assert third.new_entry_count == 1
        assert third.verify.valid_count == 3
    finally:
        await _stop_server(server_task)
