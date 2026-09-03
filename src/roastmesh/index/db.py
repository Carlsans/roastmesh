from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from roastmesh.paths import data_dir


def default_db_path() -> Path:
    """Where the GUI's index lives by default. The CLI's own default
    (`roastmesh.sqlite3`, cwd-relative -- see cli.py's DEFAULT_DB) is fine
    for a terminal user running from a project directory, but wrong for a
    double-clicked GUI with an unpredictable cwd."""
    return data_dir() / "index.sqlite3"


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL: readers (e.g. a search) don't block on a concurrent writer (e.g.
    # node serve ingesting a synced peer's feed) the way the default
    # rollback-journal mode can -- relevant now that the GUI's Network tab
    # runs its own background `node serve` writing to this same file while
    # other tabs read it. busy_timeout is the remaining belt-and-braces:
    # a genuine same-instant write/write collision waits and retries for up
    # to 5s instead of failing immediately with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    return conn


# Columns added to a table after it first shipped: `CREATE TABLE IF NOT
# EXISTS` in schema.sql only creates a *new* table with the current
# definition -- it does nothing to a table an earlier version already
# created on disk, so a column added there needs an explicit, idempotent
# ALTER TABLE here too, or every already-existing database silently never
# gets it. (table, column, sql_type) -- add a row here, never edit an
# already-shipped one.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("roasts", "title", "TEXT"),
    ("roasts", "hidden", "INTEGER NOT NULL DEFAULT 0"),
    ("sources", "author_pubkey", "TEXT"),
    # 1 = the raw .alog bytes are on disk; 0 = evicted to a search-only stub,
    # the blob deleted but the index row kept (replication.py). Defaults to 1
    # so every already-ingested source is correctly "held" until eviction says
    # otherwise.
    ("sources", "blob_local", "INTEGER NOT NULL DEFAULT 1"),
]


def _apply_added_columns(conn: sqlite3.Connection) -> None:
    for table, column, sql_type in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        except sqlite3.OperationalError as exc:
            # "duplicate column name" means a *concurrent* connection added it
            # between the PRAGMA read above and this ALTER -- a real race now
            # that serve() opens two connections at startup (the version-gated
            # refresh and the replication loop) that both migrate(). The column
            # is present either way, so this is the winner having already done
            # our work, not a failure. Any other OperationalError is real.
            if "duplicate column name" not in str(exc).lower():
                raise


def migrate(conn: sqlite3.Connection) -> None:
    schema_sql = resources.files("roastmesh.index").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    _apply_added_columns(conn)
    # Indexes on a column that only exists because of _apply_added_columns
    # (not one schema.sql's own CREATE TABLE IF NOT EXISTS could have
    # created for an already-existing table) have to be created here,
    # after that column is guaranteed to exist on both a fresh and an
    # upgraded database -- see schema.sql's comment by `sources`.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_author ON sources(author_pubkey)")
    _rebuild_fts_if_stale(conn)
    conn.commit()


def _rebuild_fts_if_stale(conn: sqlite3.Connection) -> None:
    """Recreate roasts_fts when it predates a column, and refill it.

    _apply_added_columns cannot help here: ALTER TABLE ADD COLUMN does not
    work on an FTS5 virtual table, so the only way to add `title` to a
    database that already exists is to drop the table and build it again.

    Safe to do because roasts_fts holds nothing of its own -- every column is
    a copy of one in `roasts`, so it can be rebuilt from there in full. That
    is also why it is refilled here rather than left to the version-gated
    reindex: search would otherwise return nothing at all until that ran.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(roasts_fts)")}
    if not columns or "title" in columns:
        return
    conn.execute("DROP TABLE roasts_fts")
    conn.executescript(
        resources.files("roastmesh.index").joinpath("schema.sql").read_text(encoding="utf-8")
    )
    from roastmesh.alog.machine import normalize_machine_key

    rows = conn.execute(
        """SELECT roast_id, title, beans_text, roasting_notes, cupping_notes,
                  roast_type, roaster_type_raw FROM roasts"""
    ).fetchall()
    conn.executemany(
        """INSERT INTO roasts_fts (roast_id, title, beans_text, roasting_notes,
                                    cupping_notes, roast_type, machine_display)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(r["roast_id"], r["title"], r["beans_text"], r["roasting_notes"],
          r["cupping_notes"], r["roast_type"],
          normalize_machine_key(r["roaster_type_raw"])[2]) for r in rows],
    )
    print(f"index: rebuilt the search index over {len(rows)} roast(s) so titles are "
          "searchable", flush=True)


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
