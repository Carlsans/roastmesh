CREATE TABLE IF NOT EXISTS sources (
    source_id       TEXT PRIMARY KEY,
    source_type     TEXT NOT NULL,             -- local | p2p
    source_ref      TEXT NOT NULL,             -- local filepath, or peer-relative path once p2p exists
    source_url      TEXT,
    fetched_at      TEXT NOT NULL,
    raw_path        TEXT NOT NULL,             -- path on disk where the original .alog bytes live
    content_sha256  TEXT NOT NULL UNIQUE,
    author_pubkey   TEXT                       -- publishing user's pubkey hex; NULL if unknown (see db._ADDED_COLUMNS)
);
-- idx_sources_author is NOT created here: on a database that predates this
-- column, this script's CREATE TABLE IF NOT EXISTS above is a no-op (the
-- table already exists without author_pubkey), and an index on a
-- not-yet-existing column would fail this whole executescript before
-- db._apply_added_columns ever gets a chance to ALTER TABLE it in. db.py's
-- migrate() creates this index itself, right after _apply_added_columns --
-- by then the column exists on both a fresh and an upgraded database.

CREATE TABLE IF NOT EXISTS roasts (
    roast_id            TEXT PRIMARY KEY,
    source_id            TEXT NOT NULL REFERENCES sources(source_id),
    roast_uuid             TEXT,
    roaster_type_raw         TEXT,
    machine_key                TEXT NOT NULL,
    mechanism_family             TEXT NOT NULL,
    batch_weight_in_g              REAL,
    batch_weight_out_g               REAL,
    density_g_per_l                    REAL,
    title                                 TEXT,  -- Artisan's own `title` field, often left at its default
    beans_text                           TEXT,
    roast_date                             TEXT,
    roast_epoch                              INTEGER,
    roast_type                                 TEXT,  -- e.g. "full city"; explicit tag or heuristic from DROP temp
    roasting_notes                               TEXT,
    cupping_notes                                  TEXT,
    is_user_log                                      INTEGER NOT NULL DEFAULT 0,
    hidden                                             INTEGER NOT NULL DEFAULT 0,  -- local-only: hidden from this machine's own search, never touches the feed or peers
    parse_warnings_json                                TEXT,
    raw_json                                             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roasts_machine ON roasts(machine_key);
CREATE INDEX IF NOT EXISTS idx_roasts_family ON roasts(mechanism_family);
CREATE INDEX IF NOT EXISTS idx_roasts_roast_type ON roasts(roast_type);

CREATE TABLE IF NOT EXISTS milestones (
    roast_id     TEXT NOT NULL REFERENCES roasts(roast_id),
    name          TEXT NOT NULL,
    time_s         REAL,
    bt_c            REAL,
    et_c             REAL,
    PRIMARY KEY (roast_id, name)
);

CREATE TABLE IF NOT EXISTS phase_profiles (
    roast_id            TEXT PRIMARY KEY REFERENCES roasts(roast_id),
    total_time_s          REAL,
    drying_pct              REAL,        -- CHARGE->DRY_END, % of total time
    charge_to_tp_pct        REAL,
    tp_to_dry_end_pct        REAL,
    dry_end_to_fc_pct         REAL,       -- aka "Maillard phase %"
    fc_to_drop_pct              REAL,     -- aka "Development phase %" / DTR
    dtr_pct                      REAL
);

CREATE TABLE IF NOT EXISTS note_tags (
    roast_id  TEXT NOT NULL REFERENCES roasts(roast_id),
    tag        TEXT NOT NULL,
    PRIMARY KEY (roast_id, tag)
);

-- Small key/value bookkeeping about the index itself, not any one roast --
-- currently just "what roastmesh version last refreshed every known
-- source's derived fields" (index/ingest.py's refresh_known_sources),
-- so that only runs once per version instead of on every launch.
CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    pubkey_hex          TEXT PRIMARY KEY,
    display_name        TEXT,      -- self-declared, cosmetic; NULL until learned
    machine_key         TEXT,      -- same vocabulary as roasts.machine_key
    machine_display     TEXT,      -- shown to humans; free text for a custom machine
    profile_updated_at  TEXT,
    is_favorite         INTEGER NOT NULL DEFAULT 0,  -- local-only, like roasts.hidden above:
                                                      -- never leaves this machine, never touches
                                                      -- the feed or gets sent to a peer
    first_seen          TEXT,
    last_seen            TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_machine ON users(machine_key);

CREATE TABLE IF NOT EXISTS user_likes (
    liker_pubkey    TEXT NOT NULL,
    subject_pubkey  TEXT NOT NULL,
    liked_at        TEXT NOT NULL,
    PRIMARY KEY (liker_pubkey, subject_pubkey)
);
CREATE INDEX IF NOT EXISTS idx_user_likes_subject ON user_likes(subject_pubkey);

-- Free-text search over the fields .alog has no structured equivalent for
-- (origin, process, etc. only ever show up as prose in beans_text/notes).
-- Own-copy (not "content=" external-content mode) so inserts/deletes follow
-- the same delete-then-reinsert pattern already used for milestones/note_tags.
CREATE VIRTUAL TABLE IF NOT EXISTS roasts_fts USING fts5(
    roast_id UNINDEXED,
    beans_text,
    roasting_notes,
    cupping_notes,
    roast_type,
    machine_display
);
