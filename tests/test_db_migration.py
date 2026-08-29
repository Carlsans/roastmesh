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


def _create_pre_author_pubkey_database(db_path: Path) -> None:
    """A database from before sources.author_pubkey (and the users /
    user_likes tables) existed -- everything else `title`'s already-passing
    migration test above needs, plus enough of `sources` to prove a
    pre-existing row survives gaining the new column."""
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
            title TEXT, beans_text TEXT, roast_date TEXT, roast_epoch INTEGER, roast_type TEXT,
            roasting_notes TEXT, cupping_notes TEXT, is_user_log INTEGER NOT NULL DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0, parse_warnings_json TEXT, raw_json TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO sources VALUES ('s1', 'p2p', 'abc123pub:00000001', NULL, 't', '/tmp/x.alog', 'hash1')"
    )
    conn.execute(
        "INSERT INTO roasts (roast_id, source_id, machine_key, mechanism_family, raw_json) "
        "VALUES ('r1', 's1', 'kaleido_m2', 'kaleido', '{}')"
    )
    conn.commit()
    conn.close()


def test_migrate_adds_author_pubkey_column_and_preserves_the_existing_row(tmp_path: Path) -> None:
    db_path = tmp_path / "pre_author_pubkey.sqlite3"
    _create_pre_author_pubkey_database(db_path)

    conn = connect(db_path)  # runs migrate()

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)")}
    assert "author_pubkey" in columns
    row = conn.execute("SELECT source_id, source_ref, author_pubkey FROM sources WHERE source_id = 's1'").fetchone()
    assert row["source_id"] == "s1"
    assert row["source_ref"] == "abc123pub:00000001"
    # ALTER TABLE ADD COLUMN always defaults new rows' values to NULL for a
    # row that predates the column -- ingest.py's backfill (via
    # refresh_known_sources) is what fills this in later, not the migration
    # itself.
    assert row["author_pubkey"] is None


def test_migrate_creates_idx_sources_author_on_an_upgraded_database(tmp_path: Path) -> None:
    """The index on the new column can't be created by schema.sql's own
    executescript on an upgraded database (the column doesn't exist yet at
    that point in migrate()) -- db.py has to create it itself, after
    _apply_added_columns. This is exactly the case that would break if that
    ordering ever regressed."""
    db_path = tmp_path / "pre_author_pubkey_idx.sqlite3"
    _create_pre_author_pubkey_database(db_path)

    conn = connect(db_path)

    indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_sources_author" in indexes


def test_migrate_creates_users_and_user_likes_tables_on_an_upgraded_database(tmp_path: Path) -> None:
    db_path = tmp_path / "pre_users.sqlite3"
    _create_pre_author_pubkey_database(db_path)

    conn = connect(db_path)

    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "users" in tables
    assert "user_likes" in tables
    # New tables are free (CREATE TABLE IF NOT EXISTS), but confirm they're
    # actually usable, not just present.
    conn.execute(
        "INSERT INTO users (pubkey_hex, display_name, is_favorite) VALUES ('abc123pub', 'Amber Chaff', 0)"
    )
    conn.execute(
        "INSERT INTO user_likes (liker_pubkey, subject_pubkey, liked_at) VALUES ('x', 'abc123pub', 't')"
    )
    conn.commit()


def test_migrate_author_pubkey_is_idempotent_once_the_column_already_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "new_with_author_pubkey.sqlite3"
    connect(db_path).close()

    # A second connect() (and therefore a second migrate()) must not raise
    # "duplicate column name" or fail re-creating an already-existing index.
    conn = connect(db_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)")}
    assert "author_pubkey" in columns
