import asyncio
import contextlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import iroh
import sys

import pytest

from roastmesh import net
from roastmesh.feed import append_entry, blob_path_for, read_entries
from roastmesh.identity import generate_identity
from roastmesh.peers import Peer, load_peers, save_peers
from roastmesh.quota import QuotaLimits

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURES_DIR.glob("*.alog"))[:3]


async def _start_server(identity, feed_dir, peers_path, *, profile_path=None):
    ready: asyncio.Future = asyncio.get_event_loop().create_future()
    task = asyncio.create_task(
        net.serve(identity, feed_dir, peers_path, relay=False, ready_callback=ready.set_result,
                  enable_lan_discovery=False,  # these tests exercise manual sync, not LAN discovery
                  profile_path=profile_path)
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
    # Seen *now*, not at a hardcoded date. This originally said 2026-01-01,
    # which was recent when written and is not any more: a serving node prunes
    # peers unseen for 30 days, so the fixture eventually aged out and the
    # server dropped C before it could gossip it -- a test failing because
    # time passed, which reads exactly like a broken merge.
    recent = datetime.now(timezone.utc).isoformat()
    known_peer_c = Peer(
        ticket="deadbeef-fake-ticket-for-peer-c", feed_pubkey_hex="c" * 64,
        first_seen=recent, last_seen=recent, added_via="manual",
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
    from roastmesh.index.db import connect
    from roastmesh.index.ingest import ingest_feed

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
    from roastmesh.index.db import connect
    from roastmesh.index.ingest import ingest_feed

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
    from roastmesh.index.db import connect
    from roastmesh.index.ingest import ingest_feed

    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    db_path = tmp_path / "index.sqlite3"
    _publish(feed_dir, identity, FIXTURES[:1])

    conn = connect(db_path)
    from roastmesh.index.ingest import ingest_feed
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


# ---------------------------------------------------------------------------
# get_profile: the one new wire op, and the Peer(**d) compatibility fix.
# ---------------------------------------------------------------------------

async def test_get_profile_response_returns_the_saved_profile(tmp_path: Path) -> None:
    from roastmesh.profile import update_and_sign

    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    profile_path = tmp_path / "server_profile.json"
    update_and_sign(server_identity, name="Amber Chaff", machine_key="aillio_bullet", path=profile_path)

    server_task, ticket = await _start_server(
        server_identity, server_feed_dir, tmp_path / "server_peers.json", profile_path=profile_path,
    )
    try:
        client_identity = generate_identity()
        ep = await net.bind_endpoint(client_identity, relay=False)
        try:
            ticket_obj = iroh.EndpointTicket.from_string(ticket)
            conn = await ep.connect(ticket_obj.endpoint_addr(), net.ALPN)
            response = await net._request(conn, {"op": "get_profile"})
            assert response["profile"]["name"] == "Amber Chaff"
            assert response["profile"]["pubkey"] == server_identity.public_key_hex
        finally:
            await ep.close()
    finally:
        await _stop_server(server_task)


async def test_get_profile_response_is_none_when_nothing_saved(tmp_path: Path) -> None:
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    server_task, ticket = await _start_server(
        server_identity, server_feed_dir, tmp_path / "server_peers.json",
        profile_path=tmp_path / "no_such_profile.json",
    )
    try:
        client_identity = generate_identity()
        ep = await net.bind_endpoint(client_identity, relay=False)
        try:
            ticket_obj = iroh.EndpointTicket.from_string(ticket)
            conn = await ep.connect(ticket_obj.endpoint_addr(), net.ALPN)
            response = await net._request(conn, {"op": "get_profile"})
            assert response["profile"] is None
        finally:
            await ep.close()
    finally:
        await _stop_server(server_task)


async def test_get_peers_and_get_feed_payloads_are_unaffected_by_get_profile(tmp_path: Path) -> None:
    """The one new op must not change anything about the existing three --
    same payload shape as before get_profile ever existed."""
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:1])
    server_task, ticket = await _start_server(server_identity, server_feed_dir, tmp_path / "server_peers.json")
    try:
        client_identity = generate_identity()
        ep = await net.bind_endpoint(client_identity, relay=False)
        try:
            ticket_obj = iroh.EndpointTicket.from_string(ticket)
            conn = await ep.connect(ticket_obj.endpoint_addr(), net.ALPN)
            peers_response = await net._request(conn, {"op": "get_peers"})
            assert set(peers_response.keys()) == {"peers"}
            meta_response = await net._request(conn, {"op": "get_feed_meta", "since_seq": 0})
            assert set(meta_response.keys()) == {"entries"}
        finally:
            await ep.close()
    finally:
        await _stop_server(server_task)


async def test_sync_accepts_a_valid_matching_profile(tmp_path: Path) -> None:
    from roastmesh.profile import update_and_sign

    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:1])
    profile_path = tmp_path / "server_profile.json"
    update_and_sign(
        server_identity, name="Amber Chaff", machine_key="aillio_bullet",
        machine_display="Aillio Bullet R1", likes=["c" * 64], path=profile_path,
    )

    server_task, ticket = await _start_server(
        server_identity, server_feed_dir, tmp_path / "server_peers.json", profile_path=profile_path,
    )
    try:
        client_identity = generate_identity()
        report = await net.sync_with_peer(
            ticket, client_identity, tmp_path / "client_peer_feeds", tmp_path / "client_peers.json", relay=False,
        )
        assert report.profile is not None
        assert report.profile["name"] == "Amber Chaff"
        assert report.profile["pubkey"] == server_identity.public_key_hex
        assert report.profile["likes"] == ["c" * 64]
    finally:
        await _stop_server(server_task)


async def test_sync_completes_cleanly_against_a_peer_that_does_not_know_get_profile(
    tmp_path: Path, monkeypatch,
) -> None:
    """The compat test that matters most: an un-upgraded peer -- one whose
    _build_response has no get_profile case at all -- answers
    {"error": "unknown op 'get_profile'"}, and sync_with_peer must treat
    that as "this peer has no profile", not as a sync failure."""
    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:1])

    real_build_response = net._build_response

    def old_peer_build_response(request, feed_dir, peers_path, profile_path=None, *args):
        if request.get("op") == "get_profile":
            return {"error": "unknown op 'get_profile'"}
        return real_build_response(request, feed_dir, peers_path, profile_path, *args)

    monkeypatch.setattr(net, "_build_response", old_peer_build_response)

    server_task, ticket = await _start_server(server_identity, server_feed_dir, tmp_path / "server_peers.json")
    try:
        client_identity = generate_identity()
        report = await net.sync_with_peer(
            ticket, client_identity, tmp_path / "client_peer_feeds", tmp_path / "client_peers.json", relay=False,
        )
        assert report.profile is None
        assert report.new_entry_count == 1
        assert report.verify.ok
    finally:
        await _stop_server(server_task)


async def test_sync_rejects_a_profile_whose_pubkey_does_not_match_the_connection(tmp_path: Path) -> None:
    from roastmesh.profile import update_and_sign

    server_identity = generate_identity()
    other_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:1])

    # Validly signed -- just not by the identity actually answering this
    # connection. A peer may only ever serve its OWN profile (relaying a
    # third party's would let a hostile node inject arbitrary like-graph
    # edges), so sync_with_peer must reject this without failing the sync.
    profile_path = tmp_path / "server_profile.json"
    update_and_sign(other_identity, name="Not The Server", path=profile_path)

    server_task, ticket = await _start_server(
        server_identity, server_feed_dir, tmp_path / "server_peers.json", profile_path=profile_path,
    )
    try:
        client_identity = generate_identity()
        report = await net.sync_with_peer(
            ticket, client_identity, tmp_path / "client_peer_feeds", tmp_path / "client_peers.json", relay=False,
        )
        assert report.profile is None
        assert report.new_entry_count == 1  # feed sync itself is unaffected
        assert report.verify.ok
    finally:
        await _stop_server(server_task)


async def test_sync_rejects_a_profile_with_a_bad_signature(tmp_path: Path) -> None:
    from roastmesh.profile import Profile, save_profile, sign_profile

    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:1])

    profile = Profile(pubkey="", name="Amber Chaff")
    sign_profile(server_identity, profile)
    profile.name = "Tampered Name"  # invalidates the signature without re-signing
    profile_path = tmp_path / "server_profile.json"
    save_profile(profile, profile_path)

    server_task, ticket = await _start_server(
        server_identity, server_feed_dir, tmp_path / "server_peers.json", profile_path=profile_path,
    )
    try:
        client_identity = generate_identity()
        report = await net.sync_with_peer(
            ticket, client_identity, tmp_path / "client_peer_feeds", tmp_path / "client_peers.json", relay=False,
        )
        assert report.profile is None
        assert report.new_entry_count == 1
    finally:
        await _stop_server(server_task)


async def test_auto_sync_persists_peer_profile_even_when_nothing_new_to_ingest(tmp_path: Path) -> None:
    """The trap called out in the plan: _auto_sync_discovered_peer used to
    gate its whole DB block on new_entry_count > 0, which meant a peer who
    had already published everything they ever would never got a name."""
    from roastmesh.index.db import connect
    from roastmesh.index.ingest import ingest_feed
    from roastmesh.profile import update_and_sign

    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:1])
    profile_path = tmp_path / "server_profile.json"
    update_and_sign(
        server_identity, name="Amber Chaff", machine_key="aillio_bullet",
        machine_display="Aillio Bullet R1", path=profile_path,
    )

    server_task, ticket = await _start_server(
        server_identity, server_feed_dir, tmp_path / "server_peers.json", profile_path=profile_path,
    )
    try:
        client_identity = generate_identity()
        peer_feeds_root = tmp_path / "client_peer_feeds"
        peers_path = tmp_path / "client_peers.json"
        db_path = tmp_path / "client_index.sqlite3"

        # First auto-sync: pulls the one entry (new_entry_count == 1).
        await net._auto_sync_discovered_peer(
            server_identity.public_key_hex, ticket, identity=client_identity,
            peer_feeds_root=peer_feeds_root, peers_path=peers_path, db_path=db_path, relay=False,
        )
        # Second auto-sync: nothing new to ingest (new_entry_count == 0) --
        # the profile must still be persisted.
        await net._auto_sync_discovered_peer(
            server_identity.public_key_hex, ticket, identity=client_identity,
            peer_feeds_root=peer_feeds_root, peers_path=peers_path, db_path=db_path, relay=False,
        )

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT display_name, machine_key FROM users WHERE pubkey_hex = ?",
                (server_identity.public_key_hex,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["display_name"] == "Amber Chaff"
        assert row["machine_key"] == "aillio_bullet"
    finally:
        await _stop_server(server_task)


async def test_sync_tolerates_a_gossiped_peer_dict_with_an_unknown_field(tmp_path: Path, monkeypatch) -> None:
    """Peer(**d) landmine, from the wire: a peer dict carrying a field this
    version doesn't recognize (e.g. one a newer peer added) must not raise
    TypeError and abort the whole sync."""
    from datetime import datetime, timedelta, timezone

    server_identity = generate_identity()
    server_feed_dir = tmp_path / "server_feed"
    _publish(server_feed_dir, server_identity, FIXTURES[:1])
    server_peers_path = tmp_path / "server_peers.json"
    now = datetime.now(timezone.utc).isoformat()
    known_peer_c = Peer(
        ticket="deadbeef-fake-ticket-for-peer-c", feed_pubkey_hex="c" * 64,
        first_seen=now, last_seen=now, added_via="manual",
    )
    save_peers([known_peer_c], server_peers_path)

    real_build_response = net._build_response

    def build_response_with_extra_peer_field(request, feed_dir, peers_path, profile_path=None, *args):
        response = real_build_response(request, feed_dir, peers_path, profile_path, *args)
        if request.get("op") == "get_peers":
            for peer_dict in response["peers"]:
                peer_dict["a_future_field_this_version_does_not_know_about"] = "surprise"
        return response

    monkeypatch.setattr(net, "_build_response", build_response_with_extra_peer_field)

    server_task, ticket = await _start_server(server_identity, server_feed_dir, server_peers_path)
    try:
        client_identity = generate_identity()
        client_peers_path = tmp_path / "client_peers.json"
        report = await net.sync_with_peer(
            ticket, client_identity, tmp_path / "client_peer_feeds", client_peers_path, relay=False,
        )
        assert report.new_entry_count == 1  # sync completed despite the unknown field

        client_peers = load_peers(client_peers_path)
        by_pubkey = {p.feed_pubkey_hex: p for p in client_peers}
        assert "c" * 64 in by_pubkey  # the gossiped peer still made it through
    finally:
        await _stop_server(server_task)


@pytest.mark.asyncio
async def test_auto_sync_ingests_a_mirror_the_index_never_took(tmp_path: Path) -> None:
    """Upgrading must heal a feed that was mirrored but rejected.

    Every feed published before the roastnet -> roastmesh rename failed
    verification on arrival: its entries were written to the peer mirror and
    then dropped. After the fix the feed verifies, but auto-discovery asked
    only "did new entries arrive?" -- and the answer on the next sync is 0,
    because the mirror already holds them. Ingest was skipped and those
    roasts stayed invisible for good, unless the user ran `peer sync` by hand.
    """
    from roastmesh.index.db import connect
    from roastmesh.index.ingest import ingest_feed
    from roastmesh.net import _index_is_behind_mirror

    publisher = generate_identity()
    feed_dir = tmp_path / "feed"
    for i, fixture in enumerate(sorted(FIXTURES_DIR.glob("*.alog"))[:3]):
        append_entry(feed_dir, publisher, fixture, timestamp=f"2026-01-0{i + 1}T00:00:00Z")

    # A mirror holding the publisher's entries, with an index that took none
    # of them -- exactly the state a pre-fix rejection leaves behind.
    mirror_dir = tmp_path / "peer_feeds" / publisher.public_key_hex
    shutil.copytree(feed_dir, mirror_dir)

    db_path = tmp_path / "index.sqlite3"
    conn = connect(db_path)
    try:
        assert _index_is_behind_mirror(conn, publisher.public_key_hex, mirror_dir) is True

        ingest_feed(conn, mirror_dir, expected_pubkey_hex=publisher.public_key_hex)

        # once ingested, it must stop asking for the work again every sync
        assert _index_is_behind_mirror(conn, publisher.public_key_hex, mirror_dir) is False
    finally:
        conn.close()


async def test_a_stale_ticket_falls_back_to_the_identity_dial_instead_of_hanging(monkeypatch) -> None:
    """The bug: a ticket pins addresses, those go stale, and dialling them does
    not fail -- it hangs. The identity-only fallback right below it reconnects
    in about two seconds, so the hang was not delaying success but preventing a
    recovery that would have worked. Measured as `peer sync` printing nothing
    at all for 75 seconds against a peer that was online throughout.
    """
    from roastmesh import net

    monkeypatch.setattr(net, "CONNECT_TIMEOUT_S", 0.2)
    calls: list[str] = []

    async def _never_returns(*_a, **_kw):
        calls.append("pinned")
        await asyncio.sleep(60)          # the stale-address dial, hanging

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_never_returns(), net.CONNECT_TIMEOUT_S)
    assert calls == ["pinned"]


def test_the_connect_timeout_is_bounded_and_short() -> None:
    """Pinned deliberately: the value only has to outlast a normal dial, and
    the fallback it protects takes seconds. A generous timeout here would
    reinstate the original symptom in slower form."""
    from roastmesh import net

    assert 5.0 <= net.CONNECT_TIMEOUT_S <= 30.0


async def test_serving_drops_peers_nobody_has_seen_in_a_month(tmp_path: Path) -> None:
    """`peer prune` shipped early and nothing ever called it, so in practice
    the list only grew -- measured on a real node at 764 entries, 746 learned
    through gossip and never once contacted. Gossip is worth having; a list
    that only accumulates means every node ends up carrying everyone else's
    dead addresses forever.
    """
    peers_path = tmp_path / "peers.json"
    fresh = datetime.now(timezone.utc)
    stale = fresh - timedelta(days=net.PEER_MAX_AGE_DAYS + 1)
    save_peers([
        Peer(ticket="t-fresh", feed_pubkey_hex="a" * 64,
             first_seen=fresh.isoformat(), last_seen=fresh.isoformat(), added_via="gossip"),
        Peer(ticket="t-stale", feed_pubkey_hex="b" * 64,
             first_seen=stale.isoformat(), last_seen=stale.isoformat(), added_via="gossip"),
    ], peers_path)

    task = asyncio.create_task(net._prune_peers_loop(peers_path))
    try:
        for _ in range(50):
            await asyncio.sleep(0.05)
            if len(load_peers(peers_path)) < 2:
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    remaining = {p.feed_pubkey_hex for p in load_peers(peers_path)}
    assert remaining == {"a" * 64}, "the stale peer survived, or the fresh one did not"


async def test_pruning_never_takes_down_the_node(tmp_path: Path) -> None:
    """Housekeeping runs beside the accept loop. A corrupt peers file must cost
    a log line, not the whole serve process."""
    peers_path = tmp_path / "peers.json"
    peers_path.write_text("{ this is not json", encoding="utf-8")

    task = asyncio.create_task(net._prune_peers_loop(peers_path))
    try:
        await asyncio.sleep(0.2)
        assert not task.done(), "a bad peers file killed the pruning loop"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# --- device sync: ALPN routing in _handle_connection -----------------------
#
# Fake iroh.Connection/Accepting/Incoming doubles, just detailed enough to
# drive _handle_connection's real routing logic (conn.alpn(), conn.remote_id(),
# conn.close(), conn.accept_bi()) without a real endpoint -- the plan's own
# "can be tested at the _handle_connection unit level with a fake conn".

class _FakeSendHalf:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def write_all(self, data: bytes) -> None:
        self._sink.append(data)

    async def finish(self) -> None:
        pass


class _FakeRecvHalf:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read_to_end(self, _cap: int) -> bytes:
        return self._data


class _FakeBi:
    """One fake bidirectional stream carrying exactly one JSON request in
    and collecting whatever gets written back -- net._recv_message/
    _send_message's own framing (one write_all+finish, one read_to_end)."""

    def __init__(self, request: dict) -> None:
        self._request_bytes = json.dumps(request).encode()
        self._response_chunks: list[bytes] = []

    def send(self):
        return _FakeSendHalf(self._response_chunks)

    def recv(self):
        return _FakeRecvHalf(self._request_bytes)

    def response(self) -> dict:
        return json.loads(b"".join(self._response_chunks))


class _FakeConn:
    def __init__(self, *, alpn: bytes, remote_id: str, bi_streams=None) -> None:
        self._alpn = alpn
        self._remote_id = remote_id
        self._bi_streams = list(bi_streams or [])
        self.closed = False
        self.close_args = None

    def alpn(self) -> bytes:
        return self._alpn

    def remote_id(self) -> str:
        return self._remote_id

    def close(self, code, reason) -> None:
        self.closed = True
        self.close_args = (code, reason)

    async def accept_bi(self):
        if self._bi_streams:
            return self._bi_streams.pop(0)
        raise RuntimeError("no more streams -- connection closed")


class _FakeAccepting:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def connect(self) -> _FakeConn:
        return self._conn


class _FakeIncoming:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def accept(self) -> _FakeAccepting:
        return _FakeAccepting(self._conn)


async def test_handle_connection_closes_a_sync_alpn_dial_from_an_untrusted_key(
    tmp_path: Path, monkeypatch,
) -> None:
    from roastmesh import device_sync

    # HOME alone does not isolate Path.home() on Windows -- it resolves
    # USERPROFILE there (see test_cli.py's _isolate_home).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # isolated devices.json -- nobody paired
    conn = _FakeConn(alpn=device_sync.SYNC_ALPN, remote_id="a" * 64)
    await net._handle_connection(
        _FakeIncoming(conn), tmp_path / "feed", tmp_path / "peers.json",
        devices_dir=tmp_path / "devices", device_sync_state_path=tmp_path / "device_sync_state.json",
    )
    assert conn.closed is True


async def test_handle_connection_closes_a_sync_alpn_dial_when_device_sync_is_not_configured(
    tmp_path: Path, monkeypatch,
) -> None:
    """Even a would-be-trusted key gets refused when this node wasn't given
    a devices_dir/state_path at all (enable_device_sync=False) -- there is
    no folder here to answer for."""
    from roastmesh import device_sync
    from roastmesh.devices import Device, add_device

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    pubkey = "b" * 64
    add_device(Device(pubkey=pubkey, name="trusted", platform="linux", paired_at="2026-01-01T00:00:00+00:00"))

    conn = _FakeConn(alpn=device_sync.SYNC_ALPN, remote_id=pubkey)
    await net._handle_connection(
        _FakeIncoming(conn), tmp_path / "feed", tmp_path / "peers.json",
        devices_dir=None, device_sync_state_path=None,
    )
    assert conn.closed is True


async def test_handle_connection_closes_a_pair_alpn_dial(tmp_path: Path, monkeypatch) -> None:
    """Real pairing is driven by device_sync.pair_over_lan's own dedicated
    endpoint (see its docstring) -- a PAIR_ALPN dial that lands on the
    already-running node's shared endpoint gets a clean, immediate close."""
    from roastmesh import device_sync

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    conn = _FakeConn(alpn=device_sync.PAIR_ALPN, remote_id="c" * 64)
    await net._handle_connection(
        _FakeIncoming(conn), tmp_path / "feed", tmp_path / "peers.json",
        devices_dir=tmp_path / "devices", device_sync_state_path=tmp_path / "device_sync_state.json",
    )
    assert conn.closed is True


async def test_handle_connection_serves_a_sync_alpn_dial_from_a_trusted_key(
    tmp_path: Path, monkeypatch,
) -> None:
    from roastmesh import device_sync
    from roastmesh.devices import Device, add_device

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    pubkey = "d" * 64
    add_device(Device(pubkey=pubkey, name="trusted", platform="linux", paired_at="2026-01-01T00:00:00+00:00"))

    devices_dir = tmp_path / "devices"
    devices_dir.mkdir()
    (devices_dir / "roast.alog").write_bytes(b"content")
    state_path = tmp_path / "device_sync_state.json"

    bi = _FakeBi({"op": "manifest"})
    conn = _FakeConn(alpn=device_sync.SYNC_ALPN, remote_id=pubkey, bi_streams=[bi])
    await net._handle_connection(
        _FakeIncoming(conn), tmp_path / "feed", tmp_path / "peers.json",
        devices_dir=devices_dir, device_sync_state_path=state_path,
    )
    # The request is handled on a spawned task -- give the loop a moment.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if bi._response_chunks:
            break

    assert conn.closed is False
    assert "roast.alog" in bi.response()["records"]


async def test_serve_with_enable_device_sync_false_behaves_exactly_as_before(tmp_path: Path) -> None:
    """The public-feed peer-sync path must be completely unaffected by
    disabling device sync -- a plain end-to-end sync, same as every other
    test in this file, just with the new flag explicitly off."""
    identity_a = generate_identity()
    identity_b = generate_identity()
    feed_a = tmp_path / "feed_a"
    feed_b = tmp_path / "feed_b"
    peers_a = tmp_path / "peers_a.json"
    peers_b = tmp_path / "peers_b.json"

    _publish(feed_a, identity_a, FIXTURES[:1])

    ready: asyncio.Future = asyncio.get_event_loop().create_future()
    task = asyncio.create_task(net.serve(
        identity_a, feed_a, peers_a, relay=False, ready_callback=ready.set_result,
        enable_lan_discovery=False, enable_device_sync=False,
    ))
    ticket = await asyncio.wait_for(ready, timeout=10)
    try:
        report = await net.sync_with_peer(ticket, identity_b, feed_b, peers_b, relay=False)
        assert report.new_entry_count == 1
        assert report.verify.ok
    finally:
        await _stop_server(task)
