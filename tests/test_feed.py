import hashlib
import json
from pathlib import Path

import pytest

from roastmesh.feed import (
    FeedEntry,
    append_entry,
    blob_path_for,
    feed_pubkey,
    read_entries,
    verify_feed,
    write_received_entry,
)
from roastmesh.identity import generate_identity

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURES_DIR.glob("*.alog"))[:3]


@pytest.fixture
def identity():
    return generate_identity()


def _publish_all(feed_dir: Path, identity) -> None:
    for i, path in enumerate(FIXTURES):
        append_entry(feed_dir, identity, path, timestamp=f"2026-01-0{i + 1}T00:00:00Z")


def test_append_then_read_entries_in_order(tmp_path: Path, identity) -> None:
    feed_dir = tmp_path / "feed"
    _publish_all(feed_dir, identity)

    entries = read_entries(feed_dir)
    assert [e.seq for e in entries] == list(range(len(FIXTURES)))
    assert feed_pubkey(feed_dir) == identity.public_key_hex


def test_verify_feed_succeeds_end_to_end(tmp_path: Path, identity) -> None:
    feed_dir = tmp_path / "feed"
    _publish_all(feed_dir, identity)

    result = verify_feed(feed_dir)
    assert result.ok


def test_append_entry_records_actual_file_size(tmp_path: Path, identity) -> None:
    feed_dir = tmp_path / "feed"
    _publish_all(feed_dir, identity)

    entries = read_entries(feed_dir)
    for entry, path in zip(entries, FIXTURES):
        assert entry.size_bytes == path.stat().st_size


def test_verify_catches_size_bytes_that_does_not_match_the_real_blob(tmp_path: Path, identity) -> None:
    # size_bytes is part of what's signed, so tampering it post-hoc (like
    # the other tamper tests) just trips the *signature* check -- to
    # isolate this specific check, construct an entry that a buggy client
    # legitimately signed with the wrong size_bytes in the first place.
    feed_dir = tmp_path / "feed"
    append_entry(feed_dir, identity, FIXTURES[0], timestamp="2026-01-01T00:00:00Z")
    entry0 = read_entries(feed_dir)[0]
    prev_hash = hashlib.sha256(entry0.canonical_stored_bytes()).hexdigest()

    raw_bytes = FIXTURES[1].read_bytes()
    content_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    wrong_size = len(raw_bytes) + 999

    unsigned = FeedEntry(seq=1, content_sha256=content_sha256, timestamp="2026-01-02T00:00:00Z",
                          prev_hash=prev_hash, size_bytes=wrong_size, signature="")
    signature = identity.sign(unsigned.canonical_signed_bytes()).hex()
    bad_entry = FeedEntry(seq=1, content_sha256=content_sha256, timestamp="2026-01-02T00:00:00Z",
                           prev_hash=prev_hash, size_bytes=wrong_size, signature=signature)
    write_received_entry(feed_dir, identity.public_key_hex, bad_entry, raw_bytes)

    result = verify_feed(feed_dir)
    assert not result.ok
    assert result.valid_count == 1  # entry 0 is genuinely fine
    assert "size_bytes" in result.error


def test_verify_fails_with_wrong_expected_pubkey(tmp_path: Path, identity) -> None:
    feed_dir = tmp_path / "feed"
    _publish_all(feed_dir, identity)

    other = generate_identity()
    result = verify_feed(feed_dir, expected_pubkey_hex=other.public_key_hex)
    assert not result.ok
    assert result.valid_count == 0


def test_verify_catches_tampered_entry_content_hash(tmp_path: Path, identity) -> None:
    feed_dir = tmp_path / "feed"
    _publish_all(feed_dir, identity)

    entry_path = feed_dir / "entries" / "00000001.json"
    data = json.loads(entry_path.read_text())
    data["content_sha256"] = "0" * 64
    entry_path.write_text(json.dumps(data))

    result = verify_feed(feed_dir)
    assert not result.ok
    assert result.valid_count == 1  # entry 0 still verifies; entry 1 is where it breaks
    assert "entry 1" in result.error


def test_verify_catches_corrupted_blob_bytes(tmp_path: Path, identity) -> None:
    feed_dir = tmp_path / "feed"
    _publish_all(feed_dir, identity)

    entries = read_entries(feed_dir)
    blob_path = blob_path_for(feed_dir, entries[0])
    blob_path.write_bytes(b"corrupted, not the original bytes")

    result = verify_feed(feed_dir)
    assert not result.ok
    assert result.valid_count == 0
    assert "entry 0" in result.error


def test_verify_catches_deleted_middle_entry(tmp_path: Path, identity) -> None:
    feed_dir = tmp_path / "feed"
    _publish_all(feed_dir, identity)

    (feed_dir / "entries" / "00000001.json").unlink()

    result = verify_feed(feed_dir)
    assert not result.ok
    # entry 0 verifies, then entry originally-2 (now read as seq 1 on disk
    # but its stored seq field is still 2) breaks the sequence check
    assert result.valid_count == 1


def test_verify_catches_reordered_entries(tmp_path: Path, identity) -> None:
    feed_dir = tmp_path / "feed"
    _publish_all(feed_dir, identity)

    entries_dir = feed_dir / "entries"
    e0 = json.loads((entries_dir / "00000000.json").read_text())
    e1 = json.loads((entries_dir / "00000001.json").read_text())
    (entries_dir / "00000000.json").write_text(json.dumps(e1))
    (entries_dir / "00000001.json").write_text(json.dumps(e0))

    result = verify_feed(feed_dir)
    assert not result.ok
    assert result.valid_count == 0


def test_second_publisher_cannot_write_into_first_publishers_feed_dir(tmp_path: Path, identity) -> None:
    feed_dir = tmp_path / "feed"
    _publish_all(feed_dir, identity)

    forger = generate_identity()
    with pytest.raises(ValueError):
        append_entry(feed_dir, forger, FIXTURES[0], timestamp="2026-06-01T00:00:00Z")

    # the feed is untouched -- still only the original publisher's entries
    result = verify_feed(feed_dir)
    assert result.ok
    assert result.valid_count == len(FIXTURES)


def test_write_received_entry_then_verify_succeeds(tmp_path: Path, identity) -> None:
    source_feed_dir = tmp_path / "source_feed"
    _publish_all(source_feed_dir, identity)
    entries = read_entries(source_feed_dir)

    mirror_dir = tmp_path / "mirror_feed"
    for entry in entries:
        blob_bytes = blob_path_for(source_feed_dir, entry).read_bytes()
        write_received_entry(mirror_dir, identity.public_key_hex, entry, blob_bytes)

    result = verify_feed(mirror_dir)
    assert result.ok
    assert result.valid_count == len(FIXTURES)


def test_write_received_entry_with_bogus_signature_fails_verification(tmp_path: Path, identity) -> None:
    source_feed_dir = tmp_path / "source_feed"
    _publish_all(source_feed_dir, identity)
    entry = read_entries(source_feed_dir)[0]
    blob_bytes = blob_path_for(source_feed_dir, entry).read_bytes()

    forged = FeedEntry(seq=entry.seq, content_sha256=entry.content_sha256,
                        timestamp=entry.timestamp, prev_hash=entry.prev_hash,
                        size_bytes=entry.size_bytes, signature="00" * 64)

    mirror_dir = tmp_path / "mirror_feed"
    write_received_entry(mirror_dir, identity.public_key_hex, forged, blob_bytes)

    result = verify_feed(mirror_dir)
    assert not result.ok


def test_write_received_entry_rejects_mismatched_pubkey_directory(tmp_path: Path, identity) -> None:
    source_feed_dir = tmp_path / "source_feed"
    _publish_all(source_feed_dir, identity)
    entry = read_entries(source_feed_dir)[0]
    blob_bytes = blob_path_for(source_feed_dir, entry).read_bytes()

    mirror_dir = tmp_path / "mirror_feed"
    write_received_entry(mirror_dir, identity.public_key_hex, entry, blob_bytes)

    other = generate_identity()
    with pytest.raises(ValueError):
        write_received_entry(mirror_dir, other.public_key_hex, entry, blob_bytes)


def _reanchor_to_legacy_genesis(feed_dir: Path, identity) -> None:
    """Rewrite a freshly-built feed so it is anchored to the pre-rename
    genesis constant, exactly as a feed published before roastnet became
    roastmesh is on disk. Entries are re-signed here only because the
    fixture is synthesising history the old code would have signed itself.
    """
    from roastmesh.feed import _legacy_genesis_hash

    entries = read_entries(feed_dir)
    prev = _legacy_genesis_hash(identity.public_key_hex)
    for entry in entries:
        unsigned = FeedEntry(seq=entry.seq, content_sha256=entry.content_sha256,
                             timestamp=entry.timestamp, prev_hash=prev,
                             size_bytes=entry.size_bytes, signature="")
        signed = FeedEntry(seq=entry.seq, content_sha256=entry.content_sha256,
                           timestamp=entry.timestamp, prev_hash=prev,
                           size_bytes=entry.size_bytes,
                           signature=identity.sign(unsigned.canonical_signed_bytes()).hex())
        (feed_dir / "entries" / f"{entry.seq:08d}.json").write_text(
            json.dumps(signed.__dict__, sort_keys=True), encoding="utf-8")
        prev = hashlib.sha256(signed.canonical_stored_bytes()).hexdigest()


def test_a_feed_anchored_to_the_pre_rename_genesis_still_verifies(tmp_path: Path, identity) -> None:
    """Renaming roastnet -> roastmesh changed the genesis constant every feed
    is anchored to, which silently orphaned every feed published before it:
    verify_feed reported "broken hash chain: entry 0", ingest_feed refuses
    anything that fails to verify, so peers threw away the publisher's entire
    history while `peer sync` still reported success.

    Found on a real 44-entry feed whose signatures were all individually
    valid and which no peer -- nor its own owner -- would accept.
    """
    feed_dir = tmp_path / "feed"
    _publish_all(feed_dir, identity)
    _reanchor_to_legacy_genesis(feed_dir, identity)

    result = verify_feed(feed_dir)
    assert result.error is None, result.error
    assert result.valid_count == result.total_count == len(FIXTURES)


def test_accepting_the_legacy_anchor_does_not_weaken_verification(tmp_path: Path, identity) -> None:
    """The legacy anchor is one specific alternative value, not "any prev_hash
    goes" -- a genuinely tampered chain must still be rejected."""
    feed_dir = tmp_path / "feed"
    _publish_all(feed_dir, identity)
    _reanchor_to_legacy_genesis(feed_dir, identity)

    entry_path = feed_dir / "entries" / "00000000.json"
    tampered = json.loads(entry_path.read_text(encoding="utf-8"))
    tampered["prev_hash"] = "00" * 32
    entry_path.write_text(json.dumps(tampered, sort_keys=True), encoding="utf-8")

    result = verify_feed(feed_dir)
    assert result.error is not None
    assert "entry 0" in result.error


def test_a_feed_dir_is_refused_for_a_pubkey_that_could_climb_the_tree(tmp_path: Path) -> None:
    """write_received_entry's directory is peer_feeds/<pubkey>. The pubkey
    arrives from a peer, so a traversal value would write outside the tree.
    _init_feed_dir is the last gate before the filesystem; it must refuse a
    name that is not a real pubkey. Found by an adversarial pass that wrote
    to /tmp via "../../../../tmp/x".
    """
    from roastmesh.feed import _init_feed_dir, write_received_entry

    for hostile in ("../../../../tmp/roastmesh_escape", "/tmp/roastmesh_abs", "a" * 63):
        with pytest.raises(ValueError):
            _init_feed_dir(tmp_path / hostile, hostile)

    class _Entry:
        seq = 0
        content_sha256 = "0" * 64
        size_bytes = 1
        __dict__ = {"seq": 0, "content_sha256": "0" * 64, "size_bytes": 1}

    escape = Path("/tmp/roastmesh_feed_traversal_probe")
    with pytest.raises(ValueError):
        write_received_entry(tmp_path / "peer_feeds" / "../../../../tmp/roastmesh_feed_traversal_probe",
                             "../../../../tmp/roastmesh_feed_traversal_probe", _Entry(), b"x")
    assert not escape.exists(), "a hostile pubkey wrote outside peer_feeds"
