import json
from pathlib import Path

import pytest

from datetime import datetime, timedelta, timezone

from roastmesh.feed import append_entry
from roastmesh.identity import generate_identity
from roastmesh.index.db import connect
from roastmesh.index.ingest import ingest_feed
from roastmesh.peers import Peer, prune_stale

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURES_DIR.glob("*.alog"))[:3]


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
