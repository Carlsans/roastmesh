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

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")

    try:
        raw = parse_alog_text(text)
    except AlogParseError as exc:
        return IngestResult(None, False, f"{path}: {exc}")

    source_meta = SourceMeta(source_type=source_type, source_ref=source_ref, source_url=source_url)
    record = to_roast_record(raw, source_meta, is_user_log=is_user_log)
    # An explicit machine identity (e.g. "this whole folder is my Kaleido M2
    # Lite") overrides whatever the file's own roastertype field implied --
    # the person providing the file knows their machine better than a
    # generic Artisan export string does.
    if machine_key is not None:
        record.machine_key = machine_key
    if mechanism_family is not None:
        record.mechanism_family = mechanism_family

    if existing is not None:
        # Same bytes already indexed -- don't duplicate the source/blob
        # row, but still refresh the derived `roasts` row from a fresh
        # parse. The index is meant to be a pure function of the corpus
        # (ARCHITECTURE.md), so a parser improvement (a newly-extracted
        # field, say) should take effect the next time this file happens
        # to be (re-)ingested, not require a full `reindex` to notice --
        # confirmed as a real gap: a field added to the parser after this
        # project had already shipped left every already-ingested roast
        # showing that field blank indefinitely, with no way to refresh it
        # short of wiping the whole index. Reuses the existing roast_id
        # (looked up by source_id) so this is an update, not a duplicate
        # row for the same content.
        existing_roast_id = repo.find_roast_id_by_source(conn, existing["source_id"])
        if existing_roast_id is not None:
            record.roast_id = existing_roast_id
        repo.insert_roast(conn, record, existing["source_id"])
        conn.commit()
        return IngestResult(record, True, None)

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


def refresh_known_sources(conn: sqlite3.Connection) -> list[IngestResult]:
    """Re-ingest every file this index already knows about, refreshing
    derived fields (title, roast_type, etc.) from a fresh parse -- for
    anything indexed by an older version of the parser, whose improvements
    otherwise never apply to already-known content (see ingest_file's
    "existing" branch). Unlike `reindex`, this never wipes anything: each
    row keeps its roast_id, is_user_log (read back from the current row,
    so a re-ingest can't accidentally erase "my own roasts" tagging), and
    hidden status (insert_roast preserves that one on its own).

    A source whose raw_path no longer exists on disk (a peer's feed entry
    that's been pruned away, e.g.) is skipped rather than reported as an
    error -- that's an expected, harmless state, not a problem to surface.
    """
    results = []
    for row in repo.find_all_sources(conn):
        path = Path(row["raw_path"])
        if not path.is_file():
            continue
        results.append(ingest_file(
            conn, path, source_type=row["source_type"], source_ref=row["source_ref"],
            is_user_log=bool(row["is_user_log"]),
        ))
    return results


def ingest_feed(
    conn: sqlite3.Connection,
    feed_dir: Path,
    *,
    expected_pubkey_hex: str | None = None,
    source_type: str = "p2p",
    is_user_log: bool = False,
) -> list[IngestResult]:
    """Verify a feed's signature chain, then ingest each of its valid entries.

    Refuses to ingest anything from a feed that fails verification -- don't
    trust content whose provenance doesn't check out (ARCHITECTURE.md's
    Abuse Resistance principle). If verification fails partway through, only
    the entries before the failure (the verified prefix) are ingested.

    `source_type`/`is_user_log` default to "this is a peer's feed" -- pass
    source_type="local", is_user_log=True when this is the caller's *own*
    feed (see cli.py's `feed ingest --user-log`), so it's searchable and
    filterable as "my own roasts" rather than indistinguishable from a
    peer's replicated content.
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
            source_type=source_type,
            source_ref=f"{pubkey_hex}:{entry.seq:08d}",
            is_user_log=is_user_log,
        )
        for entry in entries
    ]
    if result.error:
        results.append(IngestResult(None, False, f"feed verification stopped early: {result.error}"))
    return results
