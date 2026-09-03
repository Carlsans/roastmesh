"""Bounded, coverage-maximizing feed replication (replication.py + net.py).

Covers three things the plan calls load-bearing: the retention policy's
ordering (rarest-kept, XOR tie-break, whole-feed atomicity, pinned exempt);
relaying a third-party feed a peer never authored; and the security boundary
(pubkey-param traversal, quota on acquisition, forgery via a tampered mirror).
"""
import asyncio
import contextlib
import shutil
from pathlib import Path

import pytest

from roastmesh import net, replication
from roastmesh.feed import (append_entry, blob_path_for, feed_is_fully_held,
                            held_feeds_digest, read_entries, verify_feed)
from roastmesh.identity import generate_identity
from roastmesh.index.db import connect
from roastmesh.index.ingest import ingest_feed
from roastmesh.index import repository as repo
from roastmesh.quota import QuotaLimits
from roastmesh.replication import FeedHolding, KnownFeed, plan_retention

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURES_DIR.glob("*.alog"))[:3]


def _pk(n: int) -> str:
    return f"{n:064x}"


def _publish(feed_dir: Path, identity, paths) -> None:
    for i, path in enumerate(paths):
        append_entry(feed_dir, identity, path, timestamp=f"2026-01-{i + 1:02d}T00:00:00Z")


# ---------------------------------------------------------------------------
# Retention policy -- pure, teeth-checkable
# ---------------------------------------------------------------------------

def test_evicts_the_most_replicated_feed_first() -> None:
    my = bytes.fromhex(_pk(0))
    local = [FeedHolding(_pk(1), 1, 100, 0),   # 20 other holders
             FeedHolding(_pk(2), 1, 100, 0),   # 0 other holders (rarest)
             FeedHolding(_pk(3), 1, 100, 0)]   # 5 other holders
    known = {h.pubkey: KnownFeed(h.pubkey, 0, 100, 1) for h in local}
    holder_counts = {_pk(1): 20, _pk(2): 0, _pk(3): 5}
    plan = plan_retention(local, known, holder_counts, set(), my, budget_bytes=250)
    assert set(plan.keep) == {_pk(2), _pk(3)}      # the two rarest kept
    assert plan.evict == [_pk(1)]                   # the most-replicated evicted


def test_xor_distance_breaks_ties_between_equally_rare_feeds() -> None:
    my = bytes.fromhex(_pk(0))
    local = [FeedHolding(_pk(1), 1, 100, 0), FeedHolding(_pk(255), 1, 100, 0)]
    known = {h.pubkey: KnownFeed(h.pubkey, 0, 100, 1) for h in local}
    holder_counts = {_pk(1): 0, _pk(255): 0}       # equally rare
    plan = plan_retention(local, known, holder_counts, set(), my, budget_bytes=100)
    # my=0, so distance to _pk(1) is 1, to _pk(255) is 255 -> keep the closer.
    assert plan.keep == [_pk(1)]
    assert plan.evict == [_pk(255)]


def test_feeds_are_atomic_at_the_budget_boundary() -> None:
    my = bytes.fromhex(_pk(0))
    # Budget 150 fits one 100-byte feed but not a second -- never half of one.
    local = [FeedHolding(_pk(2), 1, 100, 0), FeedHolding(_pk(3), 1, 100, 0)]
    known = {h.pubkey: KnownFeed(h.pubkey, 0, 100, 1) for h in local}
    plan = plan_retention(local, known, {_pk(2): 0, _pk(3): 0}, set(), my, budget_bytes=150)
    assert len(plan.keep) == 1 and len(plan.evict) == 1


def test_pinned_feed_is_never_evicted_even_when_popular() -> None:
    my = bytes.fromhex(_pk(0))
    local = [FeedHolding(_pk(1), 1, 100, 0), FeedHolding(_pk(2), 1, 100, 0)]
    known = {h.pubkey: KnownFeed(h.pubkey, 0, 100, 1) for h in local}
    # _pk(1) is popular (would normally be evicted) but pinned; budget fits one.
    plan = plan_retention(local, known, {_pk(1): 99, _pk(2): 0}, {_pk(1)}, my, budget_bytes=100)
    assert _pk(1) in plan.keep
    assert plan.evict == [_pk(2)]                   # the rare-but-unpinned one goes


def test_cap_known_feeds_never_drops_a_held_or_pinned_feed() -> None:
    known = {_pk(i): KnownFeed(_pk(i), 0, 10, 1) for i in range(10)}
    holder_counts = {_pk(i): i for i in range(10)}   # higher i = more replicated
    held = {_pk(0)}
    pinned = {_pk(1)}
    drop = replication.cap_known_feeds(known, holder_counts, held, pinned, limit=5)
    assert len(known) - len(drop) >= 5
    assert _pk(0) not in drop and _pk(1) not in drop   # protected
    assert _pk(9) in drop                               # most-replicated dropped first


# ---------------------------------------------------------------------------
# Relay -- a feed a peer never authored
# ---------------------------------------------------------------------------

async def _start_server(identity, feed_dir, peers_path, peer_feeds_root):
    ready: asyncio.Future = asyncio.get_event_loop().create_future()
    task = asyncio.create_task(net.serve(
        identity, feed_dir, peers_path, relay=False, ready_callback=ready.set_result,
        enable_lan_discovery=False, peer_feeds_root=peer_feeds_root, replicate=False,
    ))
    ticket = await asyncio.wait_for(ready, timeout=10)
    return task, ticket


async def _stop(task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_relay_pulls_a_feed_the_serving_peer_never_authored(tmp_path: Path) -> None:
    # C authors a feed. A holds a mirror of it but is NOT C. B, which has never
    # contacted C, syncs A and pulls C's feed from A -- and it verifies.
    c_identity = generate_identity()
    c_feed = tmp_path / "c_feed"
    _publish(c_feed, c_identity, FIXTURES)
    c_pubkey = c_identity.public_key_hex

    a_identity = generate_identity()
    a_feeds = tmp_path / "a_feeds"
    shutil.copytree(c_feed, a_feeds / c_pubkey)      # A mirrors C's feed on disk
    assert feed_is_fully_held(a_feeds / c_pubkey)

    a_task, a_ticket = await _start_server(
        a_identity, tmp_path / "a_own_feed", tmp_path / "a_peers.json", a_feeds)
    try:
        b_identity = generate_identity()
        report = await net.sync_with_peer(
            a_ticket, b_identity, tmp_path / "b_feeds", tmp_path / "b_peers.json",
            relay=False, also_pull=[c_pubkey],
        )
        # A advertised C's feed in its digest, and B pulled it.
        advertised = {d["pubkey"] for d in report.held_feeds}
        assert c_pubkey in advertised
        assert c_pubkey in report.pulled_feeds
        mirror = tmp_path / "b_feeds" / c_pubkey
        assert len(read_entries(mirror)) == len(FIXTURES)
        from roastmesh.feed import verify_feed
        assert verify_feed(mirror, expected_pubkey_hex=c_pubkey).ok
    finally:
        await _stop(a_task)


async def test_old_peer_without_get_held_feeds_still_syncs(tmp_path: Path, monkeypatch) -> None:
    server_identity = generate_identity()
    server_feed = tmp_path / "s_feed"
    _publish(server_feed, server_identity, FIXTURES)

    real = net._build_response

    def no_held_feeds(request, feed_dir, peers_path, profile_path=None, *args):
        if request.get("op") == "get_held_feeds":
            return {"error": "unknown op 'get_held_feeds'"}
        return real(request, feed_dir, peers_path, profile_path, *args)

    monkeypatch.setattr(net, "_build_response", no_held_feeds)
    task, ticket = await _start_server(server_identity, server_feed, tmp_path / "s_peers.json",
                                       tmp_path / "s_feeds")
    try:
        report = await net.sync_with_peer(
            ticket, generate_identity(), tmp_path / "c_feeds", tmp_path / "c_peers.json",
            relay=False, also_pull=[_pk(7)],
        )
        assert report.new_entry_count == len(FIXTURES)   # own-feed sync unaffected
        assert report.held_feeds == []                    # no digest from an old peer
        assert report.pulled_feeds == []                  # nothing pulled without a digest
    finally:
        await _stop(task)


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../../../../etc/passwd", "..", "a/b", "zz" + "0" * 62, "", "ABC"])
def test_pubkey_param_traversal_is_refused_before_touching_disk(tmp_path: Path, bad: str) -> None:
    # A get_feed/get_feed_meta naming a non-pubkey must never resolve a path.
    target, error = net._feed_dir_for_request(
        {"pubkey": bad}, tmp_path / "own", tmp_path / "peer_feeds", "aa" + "0" * 62)
    assert target is None
    assert error == "invalid pubkey"


def test_get_feed_for_a_feed_we_dont_hold_returns_no_such_feed(tmp_path: Path) -> None:
    # A well-formed pubkey we simply don't hold is "nothing here", not an error.
    resp = net._build_response(
        {"op": "get_feed", "pubkey": _pk(9)}, tmp_path / "own", tmp_path / "peers.json",
        None, tmp_path / "peer_feeds", "aa" + "0" * 62)
    assert resp == {"entries": []}


async def test_oversized_third_party_feed_is_refused_by_quota(tmp_path: Path) -> None:
    c_identity = generate_identity()
    c_feed = tmp_path / "c_feed"
    _publish(c_feed, c_identity, FIXTURES)
    c_pubkey = c_identity.public_key_hex
    a_identity = generate_identity()
    a_feeds = tmp_path / "a_feeds"
    shutil.copytree(c_feed, a_feeds / c_pubkey)
    a_task, a_ticket = await _start_server(
        a_identity, tmp_path / "a_own", tmp_path / "a_peers.json", a_feeds)
    try:
        # A budget of 1 byte per file refuses every real fixture on metadata.
        report = await net.sync_with_peer(
            a_ticket, generate_identity(), tmp_path / "b_feeds", tmp_path / "b_peers.json",
            relay=False, also_pull=[c_pubkey], limits=QuotaLimits(max_bytes_per_file=1),
        )
        assert c_pubkey not in report.pulled_feeds
        assert not (tmp_path / "b_feeds" / c_pubkey).exists()
    finally:
        await _stop(a_task)


async def test_a_wholly_forged_relayed_feed_is_rejected_and_dropped(tmp_path: Path) -> None:
    # Every blob A serves is garbage -- nothing verifies, so B pulls nothing
    # and leaves no half-written mirror behind.
    c_identity = generate_identity()
    c_feed = tmp_path / "c_feed"
    _publish(c_feed, c_identity, FIXTURES)
    c_pubkey = c_identity.public_key_hex
    a_identity = generate_identity()
    a_feeds = tmp_path / "a_feeds"
    shutil.copytree(c_feed, a_feeds / c_pubkey)
    for blob in (a_feeds / c_pubkey / "blobs").glob("*.alog"):
        blob.write_bytes(b"not the signed bytes")     # corrupt them all
    a_task, a_ticket = await _start_server(
        a_identity, tmp_path / "a_own", tmp_path / "a_peers.json", a_feeds)
    try:
        report = await net.sync_with_peer(
            a_ticket, generate_identity(), tmp_path / "b_feeds", tmp_path / "b_peers.json",
            relay=False, also_pull=[c_pubkey],
        )
        assert c_pubkey not in report.pulled_feeds          # forgery detected
        assert not (tmp_path / "b_feeds" / c_pubkey).exists()  # nothing left behind
    finally:
        await _stop(a_task)


async def test_a_forged_tail_is_truncated_to_the_verified_prefix(tmp_path: Path) -> None:
    # A malicious holder serves C's real entries plus a forged LAST entry. B
    # keeps the verified prefix but must NOT retain the forged tail -- retaining
    # it would both let B re-serve unverifiable bytes and wedge since_seq so B
    # could never pull C's real later entries from a good holder.
    c_identity = generate_identity()
    c_feed = tmp_path / "c_feed"
    _publish(c_feed, c_identity, FIXTURES)
    c_pubkey = c_identity.public_key_hex
    a_identity = generate_identity()
    a_feeds = tmp_path / "a_feeds"
    shutil.copytree(c_feed, a_feeds / c_pubkey)
    entries = read_entries(a_feeds / c_pubkey)
    blob_path_for(a_feeds / c_pubkey, entries[-1]).write_bytes(b"forged tail")  # only the last
    a_task, a_ticket = await _start_server(
        a_identity, tmp_path / "a_own", tmp_path / "a_peers.json", a_feeds)
    try:
        report = await net.sync_with_peer(
            a_ticket, generate_identity(), tmp_path / "b_feeds", tmp_path / "b_peers.json",
            relay=False, also_pull=[c_pubkey],
        )
        b_mirror = tmp_path / "b_feeds" / c_pubkey
        # The valid prefix was pulled...
        assert c_pubkey in report.pulled_feeds
        # ...but only the verified entries survive on disk, and they verify clean.
        assert len(read_entries(b_mirror)) == len(FIXTURES) - 1
        assert verify_feed(b_mirror, expected_pubkey_hex=c_pubkey).ok
    finally:
        await _stop(a_task)


# ---------------------------------------------------------------------------
# Eviction keeps a searchable stub
# ---------------------------------------------------------------------------

def test_eviction_keeps_a_searchable_stub(tmp_path: Path) -> None:
    # Ingest a peer feed, then evict it: the roast stays searchable, flagged
    # not-local, and its blob file is gone.
    c_identity = generate_identity()
    c_feed = tmp_path / "c_feed"
    _publish(c_feed, c_identity, FIXTURES)
    c_pubkey = c_identity.public_key_hex
    peer_feeds = tmp_path / "peer_feeds"
    mirror = peer_feeds / c_pubkey
    shutil.copytree(c_feed, mirror)

    conn = connect(tmp_path / "index.sqlite3")
    ingest_feed(conn, mirror, expected_pubkey_hex=c_pubkey)
    before = repo.search_roasts(conn)
    assert before and all(r.blob_local for r in before)

    net._evict_feed_to_stub(conn, peer_feeds, c_pubkey)

    assert not mirror.exists()                        # blobs reclaimed
    after = repo.search_roasts(conn)
    assert len(after) == len(before)                  # still searchable
    assert all(not r.blob_local for r in after)       # flagged not-local
    assert repo.held_feed_pubkeys(conn) == set() or c_pubkey not in repo.held_feed_pubkeys(conn)
    conn.close()


def test_holder_recording_is_first_hand_only(tmp_path: Path) -> None:
    # record_sync_replication records the synced peer as a holder of the feeds
    # IT advertised -- never a third party. holder_counts excludes ourselves.
    conn = connect(tmp_path / "index.sqlite3")
    peer = "bb" + "0" * 62
    feed = "cc" + "0" * 62
    report = net.SyncReport(
        peer_pubkey_hex=peer, new_entry_count=0, verify=None, quota=None, peers_known=1,
        held_feeds=[{"pubkey": feed, "latest_seq": 2, "entry_count": 3, "total_bytes": 300}],
    )
    net.record_sync_replication(conn, report, tmp_path / "peer_feeds")
    assert repo.known_holders(conn, feed) == [peer]
    assert repo.feed_holder_counts(conn).get(feed) == 1
    conn.close()
