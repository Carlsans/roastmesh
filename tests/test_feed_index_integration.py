import json
from pathlib import Path

import pytest

from datetime import datetime, timedelta, timezone

from roastmesh.feed import append_entry, read_entries
from roastmesh.identity import generate_identity
from roastmesh.index import repository as repo
from roastmesh.index.db import connect
from roastmesh.index.ingest import ingest_feed, ingest_file
from roastmesh.peers import Peer, prune_stale

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURES_DIR.glob("*.alog"))[:3]
# Distinct content from all of FIXTURES -- needed wherever a test publishes a
# genuinely new/edited entry on top of an already-fully-published feed
# (content-hash dedup would otherwise collapse it into an existing row).
FOURTH_FIXTURE = sorted(FIXTURES_DIR.glob("*.alog"))[3]


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "index.sqlite3")
    yield connection
    connection.close()


@pytest.fixture
def published_feed(tmp_path: Path):
    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    for i, path in enumerate(FIXTURES):
        append_entry(feed_dir, identity, path, timestamp=f"2026-01-0{i + 1}T00:00:00Z")
    return feed_dir, identity


def test_ingest_feed_loads_valid_entries_as_p2p_source(conn, published_feed) -> None:
    feed_dir, identity = published_feed
    results = ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex)

    assert len(results) == len(FIXTURES)
    assert all(r.error is None for r in results)

    row_count = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
    assert row_count == len(FIXTURES)

    source_types = {row[0] for row in conn.execute("SELECT DISTINCT source_type FROM sources")}
    assert source_types == {"p2p"}


def test_ingest_feed_refuses_tampered_feed(conn, published_feed) -> None:
    feed_dir, identity = published_feed
    entry_path = feed_dir / "entries" / "00000001.json"
    data = json.loads(entry_path.read_text())
    data["content_sha256"] = "0" * 64
    entry_path.write_text(json.dumps(data))

    results = ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex)

    # only the verified prefix (entry 0) gets ingested
    row_count = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
    assert row_count == 1
    assert any(r.error for r in results)


def test_ingest_feed_refuses_wrong_pubkey(conn, published_feed) -> None:
    feed_dir, _identity = published_feed
    other = generate_identity()

    results = ingest_feed(conn, feed_dir, expected_pubkey_hex=other.public_key_hex)

    row_count = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
    assert row_count == 0
    assert len(results) == 1
    assert results[0].error is not None


def test_ingest_feed_is_dedup_idempotent(conn, published_feed) -> None:
    feed_dir, identity = published_feed
    ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex)
    second = ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex)

    assert all(r.skipped_duplicate for r in second)
    row_count = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
    assert row_count == len(FIXTURES)


def test_pruning_a_stale_peer_leaves_its_replicated_roasts_queryable(conn, published_feed) -> None:
    feed_dir, identity = published_feed
    ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex)
    row_count_before = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
    assert row_count_before == len(FIXTURES)

    now = datetime.now(timezone.utc)
    stale_peer = Peer(
        ticket="some-ticket", feed_pubkey_hex=identity.public_key_hex,
        first_seen=(now - timedelta(days=60)).isoformat(),
        last_seen=(now - timedelta(days=45)).isoformat(),
        added_via="manual",
    )
    remaining_peers = prune_stale([stale_peer], max_age_days=30, now=now)

    assert remaining_peers == []  # the peer itself is gone...
    row_count_after = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
    assert row_count_after == row_count_before  # ...but its replicated data is untouched


def test_a_superseding_entry_marks_the_original_as_superseded(conn, tmp_path: Path) -> None:
    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    append_entry(feed_dir, identity, FIXTURES[0], timestamp="2026-01-01T00:00:00Z")
    append_entry(feed_dir, identity, FIXTURES[1], timestamp="2026-01-02T00:00:00Z", supersedes=0)

    results = ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex)
    assert all(r.error is None for r in results)

    default_rows = repo.search_roasts(conn)
    assert len(default_rows) == 1
    assert default_rows[0].superseded is False

    all_rows = repo.search_roasts(conn, include_superseded=True)
    assert len(all_rows) == 2
    by_superseded = {row.superseded for row in all_rows}
    assert by_superseded == {True, False}


def test_a_superseding_entry_ingested_before_the_one_it_supersedes_still_resolves(
    conn, tmp_path: Path,
) -> None:
    # Out-of-order arrival: the entry that supersedes is ingested first (a
    # partial/replayed sync could deliver it that way), the one it
    # supersedes only arrives afterward. Resolution is a query-time join, not
    # a stored back-link, so order must not matter.
    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    append_entry(feed_dir, identity, FIXTURES[0], timestamp="2026-01-01T00:00:00Z")
    append_entry(feed_dir, identity, FIXTURES[1], timestamp="2026-01-02T00:00:00Z", supersedes=0)
    entries = read_entries(feed_dir)

    from roastmesh.feed import blob_path_for

    # Ingest entry 1 (the superseding one) alone first. It's the current
    # version regardless of whether entry 0 has arrived yet, so it must show
    # up immediately -- nothing here waits on the entry it supersedes.
    ingest_file(
        conn, blob_path_for(feed_dir, entries[1]), source_type="p2p",
        source_ref=f"{identity.public_key_hex}:{entries[1].seq:08d}",
        author_seq=entries[1].seq, supersedes_seq=entries[1].supersedes,
    )
    only_row = repo.search_roasts(conn)
    assert len(only_row) == 1
    assert only_row[0].superseded is False

    # Now the entry it supersedes arrives.
    ingest_file(
        conn, blob_path_for(feed_dir, entries[0]), source_type="p2p",
        source_ref=f"{identity.public_key_hex}:{entries[0].seq:08d}",
        author_seq=entries[0].seq, supersedes_seq=entries[0].supersedes,
    )

    default_rows = repo.search_roasts(conn)
    assert len(default_rows) == 1  # only entry 1's roast -- entry 0 resolves as superseded
    all_rows = repo.search_roasts(conn, include_superseded=True)
    assert len(all_rows) == 2


def test_refresh_known_sources_does_not_wipe_seq_info(conn, published_feed) -> None:
    from roastmesh.index.ingest import refresh_known_sources

    feed_dir, identity = published_feed
    append_entry(feed_dir, identity, FOURTH_FIXTURE, timestamp="2026-01-04T00:00:00Z", supersedes=0)
    ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex)
    before = repo.search_roasts(conn, include_superseded=True)
    superseded_before = {row.roast_id for row in before if row.superseded}
    assert superseded_before  # sanity: the fixture above actually produced a superseded row

    refresh_known_sources(conn)

    after = repo.search_roasts(conn, include_superseded=True)
    superseded_after = {row.roast_id for row in after if row.superseded}
    assert superseded_after == superseded_before
