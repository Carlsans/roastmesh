import asyncio
import contextlib
import json
import shutil
from pathlib import Path

import iroh
import sys

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
        net.serve(identity, feed_dir, peers_path, relay=False, ready_callback=ready.set_result,
                  enable_lan_discovery=False)  # these tests exercise manual sync, not LAN discovery
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs two beacons on one host; Windows does not loop limited broadcasts back "
           "locally, so LAN discovery between two Windows machines stays unverified here",
)
async def test_lan_discovery_auto_syncs_without_a_manual_sync_call(tmp_path: Path) -> None:
    """The actual proof of the feature: two serve() instances with LAN
    discovery on find each other and node B ends up with node A's entries
    in its search index -- without net.sync_with_peer ever being called
    directly anywhere in this test."""
    from roastnet.index.db import connect

    LAN_PORT = 41977  # dedicated test port -- distinct from the production default

    node_a_identity = generate_identity()
    node_a_feed_dir = tmp_path / "node_a_feed"
    _publish(node_a_feed_dir, node_a_identity, FIXTURES[:2])

    node_b_identity = generate_identity()
    node_b_db_path = tmp_path / "node_b.sqlite3"

    node_a_ready: asyncio.Future = asyncio.get_event_loop().create_future()
    node_b_ready: asyncio.Future = asyncio.get_event_loop().create_future()

    task_a = asyncio.create_task(net.serve(
        node_a_identity, node_a_feed_dir, tmp_path / "node_a_peers.json", relay=False,
        ready_callback=node_a_ready.set_result, enable_lan_discovery=True,
        lan_discovery_port=LAN_PORT, lan_discovery_interval_s=0.3,
    ))
    task_b = asyncio.create_task(net.serve(
        node_b_identity, tmp_path / "node_b_feed", tmp_path / "node_b_peers.json", relay=False,
        ready_callback=node_b_ready.set_result, enable_lan_discovery=True,
        lan_discovery_port=LAN_PORT, lan_discovery_interval_s=0.3,
        db_path=node_b_db_path, peer_feeds_root=tmp_path / "node_b_peer_feeds",
    ))
    try:
        await asyncio.wait_for(node_a_ready, timeout=10)
        await asyncio.wait_for(node_b_ready, timeout=10)

        found = False
        for _ in range(100):
            await asyncio.sleep(0.2)
            if node_b_db_path.exists():
                conn = connect(node_b_db_path)
                count = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
                conn.close()
                if count == 2:
                    found = True
                    break
        assert found, "node B never auto-synced node A's entries via LAN discovery"

        b_peers = load_peers(tmp_path / "node_b_peers.json")
        by_pubkey = {p.feed_pubkey_hex: p for p in b_peers}
        assert node_a_identity.public_key_hex in by_pubkey
        assert by_pubkey[node_a_identity.public_key_hex].added_via == "lan"
    finally:
        await _stop_server(task_a)
        await _stop_server(task_b)


async def test_serve_auto_publishes_files_dropped_in_the_watch_folder(tmp_path: Path) -> None:
    """The other half of "convivial publishing": while a node is serving
    with publish_watch_dir set, a file dropped into that folder shows up in
    the feed on its own -- no `feed publish` call anywhere in this test."""
    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    watch_dir = tmp_path / "watch"

    ready: asyncio.Future = asyncio.get_event_loop().create_future()
    task = asyncio.create_task(net.serve(
        identity, feed_dir, tmp_path / "peers.json", relay=False,
        ready_callback=ready.set_result, enable_lan_discovery=False,
        publish_watch_dir=watch_dir, publish_watch_interval_s=0.2,
    ))
    try:
        await asyncio.wait_for(ready, timeout=10)
        # the loop itself creates the folder on first scan
        for _ in range(50):
            if watch_dir.is_dir():
                break
            await asyncio.sleep(0.1)
        assert watch_dir.is_dir()

        shutil.copy(FIXTURES[0], watch_dir / FIXTURES[0].name)

        published = False
        for _ in range(50):
            await asyncio.sleep(0.2)
            if len(read_entries(feed_dir)) == 1:
                published = True
                break
        assert published, "dropped file was never auto-published"
    finally:
        await _stop_server(task)


async def test_serve_auto_ingests_watch_folder_files_as_the_users_own_roasts(tmp_path: Path) -> None:
    """The watch-folder path must also connect to search -- otherwise a
    file dropped there gets shared with every peer but never shows up in
    the user's own local search, the same disconnect `feed publish` used
    to have before it auto-ingested too."""
    from roastnet.index.db import connect

    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    watch_dir = tmp_path / "watch"
    db_path = tmp_path / "index.sqlite3"

    ready: asyncio.Future = asyncio.get_event_loop().create_future()
    task = asyncio.create_task(net.serve(
        identity, feed_dir, tmp_path / "peers.json", relay=False,
        ready_callback=ready.set_result, enable_lan_discovery=False,
        publish_watch_dir=watch_dir, publish_watch_interval_s=0.2, db_path=db_path,
    ))
    try:
        await asyncio.wait_for(ready, timeout=10)
        for _ in range(50):
            if watch_dir.is_dir():
                break
            await asyncio.sleep(0.1)
        shutil.copy(FIXTURES[0], watch_dir / FIXTURES[0].name)

        indexed_as_own = False
        for _ in range(50):
            await asyncio.sleep(0.2)
            if db_path.exists():
                conn = connect(db_path)
                try:
                    row = conn.execute("SELECT is_user_log FROM roasts").fetchone()
                finally:
                    conn.close()
                if row is not None and row["is_user_log"] == 1:
                    indexed_as_own = True
                    break
        assert indexed_as_own, "dropped file was never indexed as one of the user's own roasts"
    finally:
        await _stop_server(task)


async def test_serve_refreshes_stale_entries_on_startup(tmp_path: Path) -> None:
    """The actual "stale entries after an update" scenario a user hit:
    an already-ingested roast's title, extracted only by a newer version
    of the parser, must show up automatically the next time the app is
    opened -- without a manual reindex, and without the user needing to
    know anything happened."""
    from roastnet.index.db import connect

    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    db_path = tmp_path / "index.sqlite3"
    _publish(feed_dir, identity, FIXTURES[:1])

    conn = connect(db_path)
    from roastnet.index.ingest import ingest_feed
    ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex, is_user_log=True)
    conn.execute("UPDATE roasts SET title = NULL")  # simulate "indexed by an older version"
    conn.commit()
    conn.close()

    ready: asyncio.Future = asyncio.get_event_loop().create_future()
    task = asyncio.create_task(net.serve(
        identity, feed_dir, tmp_path / "peers.json", relay=False,
        ready_callback=ready.set_result, enable_lan_discovery=False, db_path=db_path,
    ))
    try:
        await asyncio.wait_for(ready, timeout=10)

        refreshed = False
        for _ in range(50):
            await asyncio.sleep(0.2)
            conn = connect(db_path)
            try:
                title = conn.execute("SELECT title FROM roasts").fetchone()["title"]
            finally:
                conn.close()
            if title is not None:
                refreshed = True
                break
        assert refreshed, "title was never refreshed on serve() startup"
    finally:
        await _stop_server(task)
