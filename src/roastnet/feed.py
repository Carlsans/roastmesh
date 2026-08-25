"""The append-only signed log a user's roast profiles live in.

A feed is a directory: your own local copy of your own log, and the same
structure is what verifying a peer's feed looks like once you have a copy of
it (hand-copied today; fetched over the network once Step 3 exists).

    <feed_dir>/
      pubkey.txt                    # hex Ed25519 public key -- the feed's address
      entries/00000000.json         # {seq, content_sha256, timestamp, prev_hash, size_bytes, signature}
      entries/00000001.json
      blobs/<content_sha256>.alog   # the actual profile bytes, content-addressed, dedup'd

Each entry is signed over its own fields (everything but `signature`);
`prev_hash` chains to `sha256(canonical_json(previous stored entry))`, back
to a genesis value derived from the feed's own pubkey. Tampering with,
reordering, or deleting a past entry breaks every signature/hash-chain check
after that point -- the mechanism behind ARCHITECTURE.md's "only A can
append" guarantee (Core Model), short of Hypercore's specific Merkle-tree
replication protocol.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from roastnet.identity import Identity, verify as verify_signature

_GENESIS_PREFIX = b"roastnet-feed-genesis:"


@dataclass
class FeedEntry:
    seq: int
    content_sha256: str
    timestamp: str
    prev_hash: str
    size_bytes: int
    signature: str  # hex

    def signed_fields(self) -> dict:
        return {"seq": self.seq, "content_sha256": self.content_sha256,
                "timestamp": self.timestamp, "prev_hash": self.prev_hash,
                "size_bytes": self.size_bytes}

    def canonical_signed_bytes(self) -> bytes:
        return _canonical_json(self.signed_fields())

    def canonical_stored_bytes(self) -> bytes:
        return _canonical_json({**self.signed_fields(), "signature": self.signature})


@dataclass
class FeedVerifyResult:
    valid_count: int
    total_count: int
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None and self.valid_count == self.total_count


def _canonical_json(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _genesis_hash(pubkey_hex: str) -> str:
    return hashlib.sha256(_GENESIS_PREFIX + bytes.fromhex(pubkey_hex)).hexdigest()


def _entries_dir(feed_dir: Path) -> Path:
    return feed_dir / "entries"


def _blobs_dir(feed_dir: Path) -> Path:
    return feed_dir / "blobs"


def feed_pubkey(feed_dir: Path) -> str:
    return (feed_dir / "pubkey.txt").read_text().strip()


def _init_feed_dir(feed_dir: Path, pubkey_hex: str) -> None:
    feed_dir.mkdir(parents=True, exist_ok=True)
    _entries_dir(feed_dir).mkdir(exist_ok=True)
    _blobs_dir(feed_dir).mkdir(exist_ok=True)
    pubkey_path = feed_dir / "pubkey.txt"
    if pubkey_path.exists():
        existing = pubkey_path.read_text().strip()
        if existing != pubkey_hex:
            raise ValueError(f"{feed_dir} already belongs to a different feed ({existing})")
    else:
        pubkey_path.write_text(pubkey_hex + "\n")


def read_entries(feed_dir: Path) -> list[FeedEntry]:
    entries_dir = _entries_dir(feed_dir)
    if not entries_dir.exists():
        return []
    entries = []
    for path in sorted(entries_dir.glob("*.json")):
        data = json.loads(path.read_text())
        entries.append(FeedEntry(**data))
    return entries


def append_entry(feed_dir: Path, identity: Identity, alog_path: Path, *, timestamp: str) -> FeedEntry:
    feed_dir = Path(feed_dir)
    _init_feed_dir(feed_dir, identity.public_key_hex)

    raw_bytes = Path(alog_path).read_bytes()
    content_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    blob_path = _blobs_dir(feed_dir) / f"{content_sha256}.alog"
    if not blob_path.exists():
        blob_path.write_bytes(raw_bytes)

    existing = read_entries(feed_dir)
    seq = len(existing)
    prev_hash = hashlib.sha256(existing[-1].canonical_stored_bytes()).hexdigest() if existing \
        else _genesis_hash(identity.public_key_hex)
    size_bytes = len(raw_bytes)

    unsigned = FeedEntry(seq=seq, content_sha256=content_sha256, timestamp=timestamp,
                          prev_hash=prev_hash, size_bytes=size_bytes, signature="")
    signature = identity.sign(unsigned.canonical_signed_bytes()).hex()
    entry = FeedEntry(seq=seq, content_sha256=content_sha256, timestamp=timestamp,
                       prev_hash=prev_hash, size_bytes=size_bytes, signature=signature)

    entry_path = _entries_dir(feed_dir) / f"{seq:08d}.json"
    entry_path.write_text(json.dumps(entry.__dict__, sort_keys=True))
    return entry


def verify_feed(feed_dir: Path, expected_pubkey_hex: str | None = None) -> FeedVerifyResult:
    feed_dir = Path(feed_dir)
    try:
        pubkey_hex = expected_pubkey_hex or feed_pubkey(feed_dir)
    except OSError as exc:
        return FeedVerifyResult(0, 0, f"could not read feed pubkey: {exc}")

    entries = read_entries(feed_dir)
    expected_prev = _genesis_hash(pubkey_hex)
    valid_count = 0
    for entry in entries:
        if entry.seq != valid_count:
            return FeedVerifyResult(valid_count, len(entries), f"entry {entry.seq}: out-of-order sequence number")
        if entry.prev_hash != expected_prev:
            return FeedVerifyResult(valid_count, len(entries), f"entry {entry.seq}: broken hash chain")
        try:
            signature = bytes.fromhex(entry.signature)
        except ValueError:
            return FeedVerifyResult(valid_count, len(entries), f"entry {entry.seq}: malformed signature")
        if not verify_signature(pubkey_hex, entry.canonical_signed_bytes(), signature):
            return FeedVerifyResult(valid_count, len(entries), f"entry {entry.seq}: invalid signature")

        blob_path = _blobs_dir(feed_dir) / f"{entry.content_sha256}.alog"
        if not blob_path.exists():
            return FeedVerifyResult(valid_count, len(entries), f"entry {entry.seq}: missing blob {entry.content_sha256}")
        blob_bytes = blob_path.read_bytes()
        actual_hash = hashlib.sha256(blob_bytes).hexdigest()
        if actual_hash != entry.content_sha256:
            return FeedVerifyResult(valid_count, len(entries), f"entry {entry.seq}: blob content hash mismatch")
        if len(blob_bytes) != entry.size_bytes:
            return FeedVerifyResult(valid_count, len(entries), f"entry {entry.seq}: declared size_bytes doesn't match blob")

        valid_count += 1
        expected_prev = hashlib.sha256(entry.canonical_stored_bytes()).hexdigest()

    return FeedVerifyResult(valid_count, len(entries), None)


def default_feed_dir() -> Path:
    return Path.home() / ".local" / "share" / "roastnet" / "feed"


def default_peer_feeds_root() -> Path:
    return Path.home() / ".local" / "share" / "roastnet" / "peer_feeds"


def blob_path_for(feed_dir: Path, entry: FeedEntry) -> Path:
    return _blobs_dir(Path(feed_dir)) / f"{entry.content_sha256}.alog"


def write_received_entry(feed_dir: Path, pubkey_hex: str, entry: FeedEntry, blob_bytes: bytes) -> None:
    """Write an entry + blob received from a peer into a local feed-shaped
    mirror directory, without signing -- the entry already carries the
    original author's signature. `verify_feed` is what actually checks it's
    legitimate, not this function; this only reuses the same pubkey-ownership
    guard `append_entry` uses, so a mirror directory can't be silently
    repointed at a different pubkey.
    """
    feed_dir = Path(feed_dir)
    _init_feed_dir(feed_dir, pubkey_hex)

    blob_path = _blobs_dir(feed_dir) / f"{entry.content_sha256}.alog"
    if not blob_path.exists():
        blob_path.write_bytes(blob_bytes)

    entry_path = _entries_dir(feed_dir) / f"{entry.seq:08d}.json"
    if not entry_path.exists():
        entry_path.write_text(json.dumps(entry.__dict__, sort_keys=True))
