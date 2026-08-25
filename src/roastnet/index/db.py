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


def migrate(conn: sqlite3.Connection) -> None:
    schema_sql = resources.files("roastnet.index").joinpath("schema.sql").read_text()
    conn.executescript(schema_sql)
    conn.commit()
