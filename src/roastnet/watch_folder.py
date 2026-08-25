"""Auto-publish: any `.alog` file dropped into a watched folder gets
appended to the local signed feed on its own -- no per-file Publish click
needed. Once a node is serving (and LAN/internet discovery or a synced
peer picks it up), a published entry replicates to whoever syncs with this
node -- "the folder is shared with everyone" is exactly what publishing
already means, this just removes the click.

Dedup is by content hash against the feed's own existing entries (the
feed itself is the source of truth already -- see feed.py -- so no
separate "already seen this file" bookkeeping is needed): re-scanning a
folder that hasn't changed publishes nothing new, which is what makes
polling it on a timer (net.py's _watch_publish_loop) safe and idempotent.

This only ever touches files the user themselves chose to place in their
own watch folder -- unlike a peer's replicated feed content, there's no
new trust boundary here, so none of ARCHITECTURE.md's SECURITY section
(which is about content arriving *from strangers*) applies to this path;
it's the same trust level as the existing manual "browse and publish a
file" flow.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from roastnet.feed import FeedEntry, append_entry, read_entries
from roastnet.identity import Identity
from roastnet.index.db import connect
from roastnet.index.ingest import ingest_file


def default_watch_dir() -> Path:
    return Path.home() / "RoastNetShare"


def publish_new_files(
    feed_dir: Path, identity: Identity, watch_dir: Path, *, db_path: Path | None = None,
) -> list[FeedEntry]:
    """Publish every `.alog` file directly under `watch_dir` that isn't
    already in the feed (by content hash), in filename order. Safe to call
    repeatedly -- already-published files are skipped, not re-published.

    If `db_path` is given, each newly-published file is also added to the
    local search index as one of "your own roasts" (is_user_log=True) --
    without this, publishing and search are disconnected: a file could be
    shared with every peer yet never show up in your own search results."""
    watch_dir = Path(watch_dir)
    if not watch_dir.is_dir():
        return []

    existing_hashes = {e.content_sha256 for e in read_entries(feed_dir)}
    published: list[FeedEntry] = []
    for path in sorted(watch_dir.glob("*.alog")):
        if not path.is_file():
            continue
        raw_bytes = path.read_bytes()
        content_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if content_sha256 in existing_hashes:
            continue
        entry = append_entry(feed_dir, identity, path, timestamp=datetime.now(timezone.utc).isoformat())
        existing_hashes.add(content_sha256)
        published.append(entry)
        if db_path is not None:
            conn = connect(db_path)
            try:
                ingest_file(conn, path, is_user_log=True)
            finally:
                conn.close()
    return published
