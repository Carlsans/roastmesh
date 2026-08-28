"""index/db.py's migrate(): schema.sql's `CREATE TABLE IF NOT EXISTS`
only applies to a brand-new database -- a column added to an existing
table definition needs its own explicit, idempotent ALTER TABLE
(_apply_added_columns), or a database created by an earlier version of
roastmesh never gets it. This simulates exactly that: a database built
before the `title` column existed.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from roastmesh.index.db import connect


def _create_pre_title_database(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sources (
            source_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_ref TEXT NOT NULL,
            source_url TEXT, fetched_at TEXT NOT NULL, raw_path TEXT NOT NULL,
            content_sha256 TEXT NOT NULL UNIQUE
        );
        CREATE TABLE roasts (
            roast_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, roast_uuid TEXT,
            roaster_type_raw TEXT, machine_key TEXT NOT NULL, mechanism_family TEXT NOT NULL,
            batch_weight_in_g REAL, batch_weight_out_g REAL, density_g_per_l REAL,
            beans_text TEXT, roast_date TEXT, roast_epoch INTEGER, roast_type TEXT,
            roasting_notes TEXT, cupping_notes TEXT, is_user_log INTEGER NOT NULL DEFAULT 0,
            parse_warnings_json TEXT, raw_json TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO sources VALUES ('s1', 'local', '/tmp/x.alog', NULL, 't', '/tmp/x.alog', 'hash1')"
    )
    conn.execute(
        "INSERT INTO roasts (roast_id, source_id, machine_key, mechanism_family, raw_json) "
        "VALUES ('r1', 's1', 'kaleido_m2', 'kaleido', '{}')"
    )
    conn.commit()
    conn.close()


def test_migrate_adds_title_column_to_a_database_created_before_it_existed(tmp_path: Path) -> None:
    db_path = tmp_path / "old.sqlite3"
    _create_pre_title_database(db_path)

    conn = connect(db_path)  # runs migrate()

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(roasts)")}
    assert "title" in columns
    # the pre-existing row survives the migration, title defaulting to NULL
    row = conn.execute("SELECT roast_id, title FROM roasts WHERE roast_id = 'r1'").fetchone()
    assert row["roast_id"] == "r1"
    assert row["title"] is None


def test_migrate_is_idempotent_once_the_column_already_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "new.sqlite3"
    connect(db_path).close()

    # calling connect() (and therefore migrate()) again must not raise
    # "duplicate column name" or anything else
    conn = connect(db_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(roasts)")}
    assert "title" in columns
