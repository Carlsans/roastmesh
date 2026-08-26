from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path


def default_db_path() -> Path:
    """Where the GUI's index lives by default. The CLI's own default
    (`roastnet.sqlite3`, cwd-relative -- see cli.py's DEFAULT_DB) is fine
    for a terminal user running from a project directory, but wrong for a
    double-clicked GUI with an unpredictable cwd."""
    return Path.home() / ".local" / "share" / "roastnet" / "index.sqlite3"


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
]


def _apply_added_columns(conn: sqlite3.Connection) -> None:
    for table, column, sql_type in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def migrate(conn: sqlite3.Connection) -> None:
    schema_sql = resources.files("roastnet.index").joinpath("schema.sql").read_text()
    conn.executescript(schema_sql)
    _apply_added_columns(conn)
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
