"""Shared ingest path: raw .alog bytes on disk -> parsed -> stored.

Dedup is by content_sha256 of the raw bytes: re-ingesting an identical file
is a no-op, which is what makes `reindex` safe to run repeatedly and what
will make re-ingesting a peer's feed after reconnecting idempotent once
peer ingestion exists.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from roastnet.alog.parser import AlogParseError, SourceMeta, parse_alog_text
from roastnet.alog.record import to_roast_record
from roastnet.index import repository as repo
from roastnet.models import RoastRecord


class IngestResult:
    def __init__(self, record: RoastRecord | None, skipped_duplicate: bool, error: str | None):
        self.record = record
        self.skipped_duplicate = skipped_duplicate
        self.error = error


def ingest_file(
    conn: sqlite3.Connection,
    path: Path,
    *,
    source_type: str = "local",
    source_ref: str | None = None,
    source_url: str | None = None,
    is_user_log: bool = False,
    roast_type: str | None = None,
    machine_key: str | None = None,
    mechanism_family: str | None = None,
) -> IngestResult:
    path = Path(path)
    source_ref = source_ref or str(path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        return IngestResult(None, False, f"could not read {path}: {exc}")

    content_sha256 = repo.sha256_bytes(raw_bytes)
    existing = repo.find_source_by_hash(conn, content_sha256)
    if existing is not None:
        return IngestResult(None, True, None)

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")

    try:
        raw = parse_alog_text(text)
    except AlogParseError as exc:
        return IngestResult(None, False, f"{path}: {exc}")

    source_meta = SourceMeta(source_type=source_type, source_ref=source_ref, source_url=source_url)
    record = to_roast_record(
        raw, source_meta, is_user_log=is_user_log,
        roast_type_override=roast_type, filename_hint=path.name,
    )
    # An explicit machine identity (e.g. "this whole folder is my Kaleido M2
    # Lite") overrides whatever the file's own roastertype field implied --
    # the person providing the file knows their machine better than a
    # generic Artisan export string does.
    if machine_key is not None:
        record.machine_key = machine_key
    if mechanism_family is not None:
        record.mechanism_family = mechanism_family

    source_id = str(uuid4())
    repo.insert_source(
        conn,
        source_id=source_id,
        source_type=source_type,
        source_ref=source_ref,
        source_url=source_url,
        raw_path=str(path),
        content_sha256=content_sha256,
    )
    repo.insert_roast(conn, record, source_id)
    conn.commit()
    return IngestResult(record, False, None)


def ingest_path(conn: sqlite3.Connection, path: Path, **kwargs) -> list[IngestResult]:
    """Ingest a single .alog file, or every .alog file directly under a directory."""
    path = Path(path)
    if path.is_dir():
        return [ingest_file(conn, p, **kwargs) for p in sorted(path.glob("*.alog"))]
    return [ingest_file(conn, path, **kwargs)]


def ingest_feed(
    conn: sqlite3.Connection,
    feed_dir: Path,
    *,
    expected_pubkey_hex: str | None = None,
) -> list[IngestResult]:
    """Verify a feed's signature chain, then ingest each of its valid entries.

    Refuses to ingest anything from a feed that fails verification -- don't
    trust content whose provenance doesn't check out (ARCHITECTURE.md's
    Abuse Resistance principle). If verification fails partway through, only
    the entries before the failure (the verified prefix) are ingested.
    """
    from roastnet import feed as feedmod

    feed_dir = Path(feed_dir)
    result = feedmod.verify_feed(feed_dir, expected_pubkey_hex)
    if result.valid_count == 0:
        if result.error:
            return [IngestResult(None, False, f"feed verification failed: {result.error}")]
        return []

    pubkey_hex = expected_pubkey_hex or feedmod.feed_pubkey(feed_dir)
    entries = feedmod.read_entries(feed_dir)[: result.valid_count]
    results = [
        ingest_file(
            conn,
            feedmod.blob_path_for(feed_dir, entry),
            source_type="p2p",
            source_ref=f"{pubkey_hex}:{entry.seq:08d}",
        )
        for entry in entries
    ]
    if result.error:
        results.append(IngestResult(None, False, f"feed verification stopped early: {result.error}"))
    return results
