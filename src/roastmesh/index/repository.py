from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from roastmesh.alog.machine import normalize_machine_key
from roastmesh.models import RoastRecord


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_source_by_hash(conn: sqlite3.Connection, content_sha256: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM sources WHERE content_sha256 = ?", (content_sha256,))
    return cur.fetchone()


def find_roast_id_by_source(conn: sqlite3.Connection, source_id: str) -> str | None:
    cur = conn.execute("SELECT roast_id FROM roasts WHERE source_id = ?", (source_id,))
    row = cur.fetchone()
    return row["roast_id"] if row else None


def find_all_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every currently-known source (raw_path, source_type, source_ref)
    plus its roast's is_user_log -- everything ingest.refresh_known_sources
    needs to re-ingest each one without accidentally changing what peer
    (if any) it came from."""
    return conn.execute(
        """SELECT s.raw_path, s.source_type, s.source_ref, r.is_user_log
           FROM sources s JOIN roasts r ON r.source_id = s.source_id"""
    ).fetchall()


def set_hidden(conn: sqlite3.Connection, roast_id: str, hidden: bool) -> bool:
    """Hide (or unhide) one roast from this machine's own search results.

    Purely local: never touches the feed, so it doesn't retroactively
    remove anything already replicated to a peer, and doesn't stop it
    from being replicated to a peer syncing for the first time in the
    future either -- the feed's signed, hash-chained entries can't be
    selectively skipped when serving get_feed without breaking every
    later entry's prev_hash for that peer (ARCHITECTURE.md's Core Model).
    This only ever changes what THIS machine chooses to show itself.
    Returns whether a row was actually found and updated."""
    cur = conn.execute("UPDATE roasts SET hidden = ? WHERE roast_id = ?", (int(hidden), roast_id))
    conn.commit()
    return cur.rowcount > 0


def find_hidden(conn: sqlite3.Connection, roast_id: str) -> bool | None:
    cur = conn.execute("SELECT hidden FROM roasts WHERE roast_id = ?", (roast_id,))
    row = cur.fetchone()
    return bool(row["hidden"]) if row else None


def insert_source(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    source_type: str,
    source_ref: str,
    source_url: str | None,
    raw_path: str,
    content_sha256: str,
    author_pubkey: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO sources (source_id, source_type, source_ref, source_url,
                                 fetched_at, raw_path, content_sha256, author_pubkey)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_id, source_type, source_ref, source_url,
         datetime.now(timezone.utc).isoformat(), raw_path, content_sha256, author_pubkey),
    )


def set_source_author_pubkey(conn: sqlite3.Connection, source_id: str, author_pubkey: str | None) -> None:
    """Used by ingest.ingest_file's dedup/refresh path: a source row that
    already existed before author_pubkey was introduced (or whose author
    simply needs recomputing) gets it filled in on the next time that same
    content happens to be (re-)ingested -- the same self-healing pattern
    already used for `roasts`' derived fields (see insert_roast)."""
    conn.execute("UPDATE sources SET author_pubkey = ? WHERE source_id = ?", (author_pubkey, source_id))


def claim_orphan_local_sources(conn: sqlite3.Connection, author_pubkey: str) -> int:
    """Attribute local sources that have no author yet to `author_pubkey`,
    returning how many rows were claimed.

    A local source is one ingested from this machine's own filesystem, so it
    is yours by construction. It ends up with a NULL author when it was
    ingested *before* an identity existed -- entirely normal, since `ingest`
    deliberately never creates a key as a side effect, and pointing the tool
    at a folder of .alog files is a plausible very first thing to do. Without
    this, those roasts stay unattributable until the next version-gated
    refresh, so they are invisible to `search --user` and to the
    owner-machine fallback.

    p2p rows are never touched: their author is whoever signed the feed.
    """
    cursor = conn.execute(
        "UPDATE sources SET author_pubkey = ? "
        "WHERE author_pubkey IS NULL AND source_type = 'local'",
        (author_pubkey,),
    )
    conn.commit()
    return cursor.rowcount


def insert_roast(conn: sqlite3.Connection, record: RoastRecord, source_id: str) -> None:
    # `hidden` is deliberately never in this column list and never comes
    # from `record` -- there's no ingest-time concept of "hidden", only
    # set_hidden(). INSERT OR REPLACE deletes-then-reinserts the row, so
    # any column left out of the statement would silently reset to its
    # schema default (0, unhidden) on every re-ingest -- confirmed as a
    # real bug this would have caused: refreshing an already-hidden
    # roast's derived fields (see ingest.py's self-healing re-ingest)
    # would have silently un-hidden it. The subquery carries the existing
    # value forward (0 for a genuinely new roast_id, since the subquery
    # then finds no row).
    conn.execute(
        """INSERT OR REPLACE INTO roasts
           (roast_id, source_id, roast_uuid, roaster_type_raw, machine_key, mechanism_family,
            batch_weight_in_g, batch_weight_out_g, density_g_per_l, title, beans_text, roast_date,
            roast_epoch, roast_type, roasting_notes, cupping_notes, is_user_log, hidden,
            parse_warnings_json, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   COALESCE((SELECT hidden FROM roasts WHERE roast_id = ?), 0), ?, ?)""",
        (
            record.roast_id, source_id, record.roast_uuid, record.roaster_type_raw,
            record.machine_key, record.mechanism_family, record.batch_weight_in_g,
            record.batch_weight_out_g, record.density_g_per_l, record.title, record.beans_text,
            record.roast_date, record.roast_epoch, record.roast_type, record.roasting_notes,
            record.cupping_notes, int(record.is_user_log),
            record.roast_id,
            json.dumps(record.parse_warnings), json.dumps(record.to_dict()),
        ),
    )

    conn.execute("DELETE FROM milestones WHERE roast_id = ?", (record.roast_id,))
    for m in record.milestones:
        conn.execute(
            "INSERT INTO milestones (roast_id, name, time_s, bt_c, et_c) VALUES (?, ?, ?, ?, ?)",
            (record.roast_id, m.name, m.time_s, m.bt_c, m.et_c),
        )

    conn.execute("DELETE FROM phase_profiles WHERE roast_id = ?", (record.roast_id,))
    if record.phase_profile:
        p = record.phase_profile
        conn.execute(
            """INSERT INTO phase_profiles
               (roast_id, total_time_s, drying_pct, charge_to_tp_pct, tp_to_dry_end_pct,
                dry_end_to_fc_pct, fc_to_drop_pct, dtr_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.roast_id, p.get("total_time_s"), p.get("drying_pct"), p.get("charge_to_tp_pct"),
             p.get("tp_to_dry_end_pct"), p.get("dry_end_to_fc_pct"),
             p.get("fc_to_drop_pct"), p.get("dtr_pct")),
        )

    conn.execute("DELETE FROM note_tags WHERE roast_id = ?", (record.roast_id,))
    for tag in record.note_tags:
        conn.execute(
            "INSERT OR IGNORE INTO note_tags (roast_id, tag) VALUES (?, ?)",
            (record.roast_id, tag),
        )

    conn.execute("DELETE FROM roasts_fts WHERE roast_id = ?", (record.roast_id,))
    _, _, machine_display = normalize_machine_key(record.roaster_type_raw)
    conn.execute(
        """INSERT INTO roasts_fts (roast_id, title, beans_text, roasting_notes,
                                    cupping_notes, roast_type, machine_display)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (record.roast_id, record.title, record.beans_text, record.roasting_notes,
         record.cupping_notes, record.roast_type, machine_display),
    )


@dataclass
class RoastSearchRow:
    roast_id: str
    machine_key: str
    mechanism_family: str
    roast_type: str | None
    batch_weight_in_g: float | None
    density_g_per_l: float | None
    title: str | None
    beans_text: str | None
    roast_date: str | None
    dtr_pct: float | None
    total_time_s: float | None
    drop_bt_c: float | None
    source_ref: str
    source_type: str
    raw_path: str
    is_user_log: bool
    hidden: bool
    author_pubkey: str | None
    # False for a roast whose blob was evicted to a search-only stub
    # (replication.py): still findable, bytes fetched on demand when opened.
    blob_local: bool = True


def _fts_query(text: str) -> str:
    # Treat the query as a set of required terms (AND, not FTS5's default OR)
    # -- for a search box, "washed ethiopian" finding roasts that match only
    # one of those words is a worse result than finding none.
    terms = [t.replace('"', '""') for t in text.split()]
    return " AND ".join(f'"{t}"' for t in terms) if terms else ""


def search_roasts(
    conn: sqlite3.Connection,
    *,
    text: str | None = None,
    machine_key: str | None = None,
    roast_type: str | None = None,
    dtr_min: float | None = None,
    dtr_max: float | None = None,
    drop_bt_min: float | None = None,
    after_second_crack: bool | None = None,
    own_only: bool = False,
    include_hidden: bool = False,
    user_pubkey: str | None = None,
    favorites_only: bool = False,
) -> list[RoastSearchRow]:
    sql = """
        SELECT r.roast_id, r.machine_key, r.mechanism_family, r.roast_type,
               r.batch_weight_in_g, r.density_g_per_l, r.title, r.beans_text, r.roast_date,
               r.is_user_log, r.hidden, s.source_ref, s.source_type, s.raw_path, s.author_pubkey,
               s.blob_local,
               p.dtr_pct, p.total_time_s,
               (SELECT bt_c FROM milestones m WHERE m.roast_id = r.roast_id AND m.name = 'DROP') AS drop_bt_c,
               (SELECT bt_c FROM milestones m WHERE m.roast_id = r.roast_id AND m.name = 'SC_START') AS sc_start_bt_c
        FROM roasts r
        JOIN sources s ON s.source_id = r.source_id
        LEFT JOIN phase_profiles p ON p.roast_id = r.roast_id
        LEFT JOIN users u ON u.pubkey_hex = s.author_pubkey
    """
    # ^ Must be joined here, ahead of the `if text:` block below -- that one
    # string-concatenates the FTS join onto `sql` right before `WHERE` gets
    # appended, so any join added after this point would land after WHERE
    # and produce malformed SQL.
    conditions: list[str] = []
    params: list = []

    if text:
        sql += " JOIN roasts_fts f ON f.roast_id = r.roast_id"
        conditions.append("roasts_fts MATCH ?")
        params.append(_fts_query(text))
    if machine_key:
        # Widens, never narrows, an existing --machine call: a roast whose
        # own .alog recorded no machine (machine_key == "unknown") is still
        # found by matching its owner's declared machine instead -- chosen
        # over requiring both to differ, since "unknown" AND a real owner
        # machine_key is the only case with no signal to lose.
        conditions.append("(r.machine_key = ? OR (r.machine_key = 'unknown' AND u.machine_key = ?))")
        params.append(machine_key)
        params.append(machine_key)
    if user_pubkey:
        # s.author_pubkey (not u.pubkey_hex) is the source of truth for who
        # published a roast -- a `users` row might not exist yet for a
        # never-synced author, and that must not silently hide their roasts
        # from a direct --user lookup by their own pubkey.
        conditions.append("s.author_pubkey = ?")
        params.append(user_pubkey)
    if favorites_only:
        conditions.append("u.is_favorite = 1")
    if roast_type:
        conditions.append("r.roast_type = ?")
        params.append(roast_type)
    if dtr_min is not None:
        conditions.append("p.dtr_pct >= ?")
        params.append(dtr_min)
    if dtr_max is not None:
        conditions.append("p.dtr_pct <= ?")
        params.append(dtr_max)
    if drop_bt_min is not None:
        conditions.append(
            "(SELECT bt_c FROM milestones m WHERE m.roast_id = r.roast_id AND m.name = 'DROP') >= ?"
        )
        params.append(drop_bt_min)
    if own_only:
        conditions.append("r.is_user_log = 1")
    if not include_hidden:
        conditions.append("r.hidden = 0")

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    cur = conn.execute(sql, params)
    rows = [
        RoastSearchRow(
            roast_id=row["roast_id"],
            machine_key=row["machine_key"],
            mechanism_family=row["mechanism_family"],
            roast_type=row["roast_type"],
            batch_weight_in_g=row["batch_weight_in_g"],
            density_g_per_l=row["density_g_per_l"],
            title=row["title"],
            beans_text=row["beans_text"],
            roast_date=row["roast_date"],
            dtr_pct=row["dtr_pct"],
            total_time_s=row["total_time_s"],
            drop_bt_c=row["drop_bt_c"],
            source_ref=row["source_ref"],
            source_type=row["source_type"],
            raw_path=row["raw_path"],
            is_user_log=bool(row["is_user_log"]),
            hidden=bool(row["hidden"]),
            author_pubkey=row["author_pubkey"],
            blob_local=bool(row["blob_local"]),
        )
        for row in cur.fetchall()
        # after_second_crack can't be expressed as a plain SQL predicate
        # without repeating the correlated subquery a third time, so it's
        # applied here instead.
        if after_second_crack is None or (
            row["sc_start_bt_c"] is not None
            and row["drop_bt_c"] is not None
            and row["drop_bt_c"] >= row["sc_start_bt_c"]
        ) == after_second_crack
    ]
    return rows


def load_full_record(conn: sqlite3.Connection, roast_id: str) -> dict | None:
    cur = conn.execute("SELECT raw_json FROM roasts WHERE roast_id = ?", (roast_id,))
    row = cur.fetchone()
    return json.loads(row["raw_json"]) if row else None


def find_ids_by_prefix(conn: sqlite3.Connection, roast_id_prefix: str) -> list[str]:
    """Resolve a (possibly truncated, e.g. the 8 characters a results table
    displays) roast_id back to full id(s) -- `show` uses this so a user
    never has to copy-paste a full UUID out of the GUI."""
    cur = conn.execute("SELECT roast_id FROM roasts WHERE roast_id LIKE ? || '%'", (roast_id_prefix,))
    return [row["roast_id"] for row in cur.fetchall()]


def find_raw_path(conn: sqlite3.Connection, roast_id: str) -> str | None:
    cur = conn.execute(
        "SELECT s.raw_path FROM roasts r JOIN sources s ON s.source_id = r.source_id WHERE r.roast_id = ?",
        (roast_id,),
    )
    row = cur.fetchone()
    return row["raw_path"] if row else None


# ---------------------------------------------------------------------------
# Users, favorites, likes
# ---------------------------------------------------------------------------

@dataclass
class UserRow:
    pubkey_hex: str
    display_name: str | None
    machine_key: str | None
    machine_display: str | None
    profile_updated_at: str | None
    is_favorite: bool
    first_seen: str | None
    last_seen: str | None
    roast_count: int
    like_count: int


def upsert_user_from_profile(
    conn: sqlite3.Connection,
    *,
    pubkey_hex: str,
    display_name: str | None,
    machine_key: str | None,
    machine_display: str | None,
    profile_updated_at: str | None,
    seen_at: str | None = None,
) -> None:
    """Insert or refresh a peer's row from a *signature-verified* profile --
    the caller (profile.verify_profile) is trusted to have already checked
    that before this is called; this function itself does no verification.

    `is_favorite` is deliberately never set here: it's local-only (see
    schema.sql's comment on users.is_favorite) and never arrives from a
    peer. `first_seen` is likewise left alone on a conflict -- only a brand
    new row gets it -- while `last_seen` always advances.
    """
    seen_at = seen_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO users (pubkey_hex, display_name, machine_key, machine_display,
                               profile_updated_at, is_favorite, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?)
           ON CONFLICT(pubkey_hex) DO UPDATE SET
               display_name = excluded.display_name,
               machine_key = excluded.machine_key,
               machine_display = excluded.machine_display,
               profile_updated_at = excluded.profile_updated_at,
               last_seen = excluded.last_seen""",
        (pubkey_hex, display_name, machine_key, machine_display, profile_updated_at, seen_at, seen_at),
    )
    conn.commit()


def find_user(conn: sqlite3.Connection, pubkey_hex: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM users WHERE pubkey_hex = ?", (pubkey_hex,))
    return cur.fetchone()


def find_user_pubkeys_by_prefix(conn: sqlite3.Connection, pubkey_prefix: str) -> list[str]:
    """Resolve a (possibly truncated, e.g. the 8-character prefix the CLI
    and GUI display) pubkey back to full pubkey(s) -- same shape as
    find_ids_by_prefix for roast ids, so the CLI's `user show`/`favorite`/
    `like` commands can accept a prefix the way roast commands accept a
    roast_id prefix. Spans the same two sources as list_users(with_roasts_only
    =False): a `users` row (a synced profile, or one created bare by
    favoriting/liking) and `sources.author_pubkey` (an ingested roast from
    someone whose profile has never synced) -- a pubkey known only from one
    of the two must still resolve."""
    cur = conn.execute(
        """
        SELECT DISTINCT pubkey_hex FROM (
            SELECT pubkey_hex FROM users
            UNION
            SELECT DISTINCT author_pubkey AS pubkey_hex FROM sources WHERE author_pubkey IS NOT NULL
        )
        WHERE pubkey_hex LIKE ? || '%'
        """,
        (pubkey_prefix,),
    )
    return [row["pubkey_hex"] for row in cur.fetchall()]


def ensure_user(conn: sqlite3.Connection, pubkey_hex: str) -> None:
    """Create a bare `users` row for `pubkey_hex` if one doesn't already
    exist; a no-op (never overwrites) if it does. Needed because
    set_user_favorite/add_user_like never create a row themselves (a pubkey
    known only from an ingested roast's sources.author_pubkey has no
    `users` row yet) -- this is what makes list_users' documented "or
    created bare by favoriting" actually true. Called by the CLI's
    `user favorite`/`user like` before the real mutation."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO users
               (pubkey_hex, display_name, machine_key, machine_display,
                profile_updated_at, is_favorite, first_seen, last_seen)
           VALUES (?, NULL, NULL, NULL, NULL, 0, ?, ?)""",
        (pubkey_hex, now, now),
    )
    conn.commit()


def set_user_favorite(conn: sqlite3.Connection, pubkey_hex: str, is_favorite: bool) -> bool:
    """Favorite/unfavorite a user this index already knows about -- local-
    only (schema.sql's comment on users.is_favorite: never leaves this
    machine), same style as roasts.hidden's set_hidden. Unlike
    upsert_user_from_profile this never creates a row: favoriting only
    makes sense for a pubkey already known from a synced profile or an
    ingested roast. Returns whether a row was actually found and updated."""
    cur = conn.execute(
        "UPDATE users SET is_favorite = ? WHERE pubkey_hex = ?", (int(is_favorite), pubkey_hex)
    )
    conn.commit()
    return cur.rowcount > 0


def add_user_like(
    conn: sqlite3.Connection, liker_pubkey: str, subject_pubkey: str, liked_at: str | None = None
) -> None:
    """A `user_likes` row is only ever written from the local user's own
    like action or from a signature-verified profile's `likes` list
    (profile.py) -- never from an unverified claim (see this table's
    invariant in the plan). Likes are public and attributable by design, so
    this is a plain replace-on-conflict rather than a toggle."""
    liked_at = liked_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO user_likes (liker_pubkey, subject_pubkey, liked_at) VALUES (?, ?, ?)",
        (liker_pubkey, subject_pubkey, liked_at),
    )
    conn.commit()


def remove_user_like(conn: sqlite3.Connection, liker_pubkey: str, subject_pubkey: str) -> bool:
    cur = conn.execute(
        "DELETE FROM user_likes WHERE liker_pubkey = ? AND subject_pubkey = ?",
        (liker_pubkey, subject_pubkey),
    )
    conn.commit()
    return cur.rowcount > 0


def list_users(
    conn: sqlite3.Connection,
    *,
    machine_key: str | None = None,
    favorites_only: bool = False,
    with_roasts_only: bool = True,
) -> list[UserRow]:
    """Every known user, with roast and like counts.

    "Known" spans two sources that don't always overlap: a `users` row
    (learned from a synced profile, or created bare by favoriting) and a
    `sources.author_pubkey` (someone whose roast got ingested before their
    profile ever synced -- expected, since profiles are self-served and not
    relayed). `with_roasts_only=True` (the user's chosen default -- "Only
    users with roasts, with a toggle to show all known peers") restricts
    the candidate set to the second; False unions in the first as well, so
    a favorited-but-not-yet-publishing peer still shows up.
    """
    if with_roasts_only:
        candidates_sql = "SELECT DISTINCT author_pubkey AS pubkey_hex FROM sources WHERE author_pubkey IS NOT NULL"
    else:
        candidates_sql = """
            SELECT pubkey_hex FROM users
            UNION
            SELECT DISTINCT author_pubkey AS pubkey_hex FROM sources WHERE author_pubkey IS NOT NULL
        """

    sql = f"""
        SELECT c.pubkey_hex, u.display_name, u.machine_key, u.machine_display,
               u.profile_updated_at, COALESCE(u.is_favorite, 0) AS is_favorite,
               u.first_seen, u.last_seen,
               COUNT(DISTINCT r.roast_id) AS roast_count,
               COUNT(DISTINCT l.liker_pubkey) AS like_count
        FROM ({candidates_sql}) c
        LEFT JOIN users u ON u.pubkey_hex = c.pubkey_hex
        LEFT JOIN sources s ON s.author_pubkey = c.pubkey_hex
        LEFT JOIN roasts r ON r.source_id = s.source_id AND r.hidden = 0
        LEFT JOIN user_likes l ON l.subject_pubkey = c.pubkey_hex
    """
    conditions: list[str] = []
    params: list = []
    if machine_key:
        conditions.append("u.machine_key = ?")
        params.append(machine_key)
    if favorites_only:
        conditions.append("COALESCE(u.is_favorite, 0) = 1")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " GROUP BY c.pubkey_hex"

    cur = conn.execute(sql, params)
    return [
        UserRow(
            pubkey_hex=row["pubkey_hex"],
            display_name=row["display_name"],
            machine_key=row["machine_key"],
            machine_display=row["machine_display"],
            profile_updated_at=row["profile_updated_at"],
            is_favorite=bool(row["is_favorite"]),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            roast_count=row["roast_count"],
            like_count=row["like_count"],
        )
        for row in cur.fetchall()
    ]


def find_distinct_machine_keys(conn: sqlite3.Connection) -> list[str]:
    """Every machine_key currently present among ingested roasts, sorted,
    for a search/settings autocomplete -- there is no DISTINCT query
    anywhere else in this codebase today."""
    cur = conn.execute(
        "SELECT DISTINCT machine_key FROM roasts WHERE machine_key IS NOT NULL ORDER BY machine_key"
    )
    return [row["machine_key"] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Replication ledger + stubs (replication.py)
# ---------------------------------------------------------------------------

def upsert_known_feed(
    conn: sqlite3.Connection,
    feed_pubkey: str,
    *,
    latest_seq: int | None = None,
    total_bytes: int | None = None,
    entry_count: int | None = None,
    held_local: bool | None = None,
    pinned: bool | None = None,
) -> None:
    """Record (or update) a feed in the ledger. Only the fields passed
    non-None are written, so merging a peer's digest can advance latest_seq
    without clobbering our own held_local/pinned flags, and marking a feed
    held/pinned locally doesn't reset a peer-reported latest_seq. A brand-new
    feed defaults to held_local=0, pinned=0 -- a hint until we actually fetch
    it."""
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT latest_seq, total_bytes, entry_count, held_local, pinned "
        "FROM known_feeds WHERE feed_pubkey = ?",
        (feed_pubkey,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO known_feeds (feed_pubkey, latest_seq, total_bytes, entry_count, "
            "held_local, pinned, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (feed_pubkey, latest_seq, total_bytes, entry_count,
             int(bool(held_local)), int(bool(pinned)), now),
        )
    else:
        conn.execute(
            "UPDATE known_feeds SET latest_seq = ?, total_bytes = ?, entry_count = ?, "
            "held_local = ?, pinned = ?, last_updated = ? WHERE feed_pubkey = ?",
            (
                latest_seq if latest_seq is not None else row["latest_seq"],
                total_bytes if total_bytes is not None else row["total_bytes"],
                entry_count if entry_count is not None else row["entry_count"],
                int(held_local) if held_local is not None else row["held_local"],
                int(pinned) if pinned is not None else row["pinned"],
                now, feed_pubkey,
            ),
        )
    conn.commit()


def record_feed_holder(
    conn: sqlite3.Connection, feed_pubkey: str, holder_pubkey: str, latest_seq: int | None
) -> None:
    """Note that `holder_pubkey` reports holding `feed_pubkey`. Caller is
    responsible for the trust gate (only record holders we could reach -- see
    net.record_feed_holder) so a stranger can't inflate a feed's apparent
    replication to get it evicted everywhere."""
    conn.execute(
        "INSERT INTO feed_holders (feed_pubkey, holder_pubkey, latest_seq, last_reported) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(feed_pubkey, holder_pubkey) DO UPDATE SET "
        "latest_seq = excluded.latest_seq, last_reported = excluded.last_reported",
        (feed_pubkey, holder_pubkey, latest_seq, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def feed_holder_counts(conn: sqlite3.Connection, *, exclude_holder: str | None = None) -> dict[str, int]:
    """replica estimate per feed: how many *other* peers hold it. Excludes our
    own pubkey so a feed we alone hold reads as rarity 0 (must-keep), not 1."""
    if exclude_holder is None:
        cur = conn.execute("SELECT feed_pubkey, COUNT(*) c FROM feed_holders GROUP BY feed_pubkey")
    else:
        cur = conn.execute(
            "SELECT feed_pubkey, COUNT(*) c FROM feed_holders WHERE holder_pubkey != ? "
            "GROUP BY feed_pubkey",
            (exclude_holder,),
        )
    return {row["feed_pubkey"]: row["c"] for row in cur.fetchall()}


def known_holders(conn: sqlite3.Connection, feed_pubkey: str) -> list[str]:
    """Holder pubkeys for a feed, most-recently-reported first -- who to try
    when fetching an evicted stub's bytes on demand."""
    cur = conn.execute(
        "SELECT holder_pubkey FROM feed_holders WHERE feed_pubkey = ? ORDER BY last_reported DESC",
        (feed_pubkey,),
    )
    return [row["holder_pubkey"] for row in cur.fetchall()]


def load_known_feeds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT feed_pubkey, latest_seq, total_bytes, entry_count, held_local, pinned "
        "FROM known_feeds"
    ).fetchall()


def held_feed_pubkeys(conn: sqlite3.Connection) -> set[str]:
    return {r["feed_pubkey"] for r in conn.execute(
        "SELECT feed_pubkey FROM known_feeds WHERE held_local = 1")}


def pinned_feed_pubkeys(conn: sqlite3.Connection) -> set[str]:
    return {r["feed_pubkey"] for r in conn.execute(
        "SELECT feed_pubkey FROM known_feeds WHERE pinned = 1")}


def set_feed_pinned(conn: sqlite3.Connection, feed_pubkey: str, pinned: bool) -> None:
    upsert_known_feed(conn, feed_pubkey, pinned=pinned)


def set_blobs_local(conn: sqlite3.Connection, feed_pubkey: str, blob_local: bool) -> int:
    """Flip the blob_local flag on every p2p source authored by `feed_pubkey`
    and mirror it into the ledger. Returns the number of source rows touched.
    Evicting to a stub deletes the actual blob files separately (net.py); this
    only records that they are gone so search can flag the roast 'not
    downloaded' and on-demand fetch knows to re-materialize it."""
    cur = conn.execute(
        "UPDATE sources SET blob_local = ? WHERE author_pubkey = ?",
        (int(blob_local), feed_pubkey),
    )
    conn.execute(
        "UPDATE known_feeds SET held_local = ? WHERE feed_pubkey = ?",
        (int(blob_local), feed_pubkey),
    )
    conn.commit()
    return cur.rowcount


def is_blob_local(conn: sqlite3.Connection, roast_id: str) -> bool | None:
    row = conn.execute(
        "SELECT s.blob_local FROM roasts r JOIN sources s ON s.source_id = r.source_id "
        "WHERE r.roast_id = ?",
        (roast_id,),
    ).fetchone()
    return bool(row["blob_local"]) if row is not None else None


def feed_pubkey_for_roast(conn: sqlite3.Connection, roast_id: str) -> str | None:
    row = conn.execute(
        "SELECT s.author_pubkey FROM roasts r JOIN sources s ON s.source_id = r.source_id "
        "WHERE r.roast_id = ?",
        (roast_id,),
    ).fetchone()
    return row["author_pubkey"] if row else None


def delete_known_feeds(conn: sqlite3.Connection, pubkeys: set[str]) -> None:
    """Drop ledger + holder rows for feeds being forgotten (cap_known_feeds).
    Never deletes any `sources`/`roasts` row -- those are the real index; this
    only prunes the bounded gossip ledger about feeds we neither hold nor pin."""
    for pk in pubkeys:
        conn.execute("DELETE FROM known_feeds WHERE feed_pubkey = ?", (pk,))
        conn.execute("DELETE FROM feed_holders WHERE feed_pubkey = ?", (pk,))
    conn.commit()


def cap_feed_holders(conn: sqlite3.Connection, feed_pubkey: str, limit: int) -> None:
    """Keep at most `limit` holder rows for one feed, the most recently
    reported -- a replica estimate only needs enough to answer 'rare or not'."""
    keep = conn.execute(
        "SELECT holder_pubkey FROM feed_holders WHERE feed_pubkey = ? "
        "ORDER BY last_reported DESC LIMIT ?",
        (feed_pubkey, limit),
    ).fetchall()
    if len(keep) < limit:
        return
    kept = {r["holder_pubkey"] for r in keep}
    all_holders = [r["holder_pubkey"] for r in conn.execute(
        "SELECT holder_pubkey FROM feed_holders WHERE feed_pubkey = ?", (feed_pubkey,))]
    for h in all_holders:
        if h not in kept:
            conn.execute(
                "DELETE FROM feed_holders WHERE feed_pubkey = ? AND holder_pubkey = ?",
                (feed_pubkey, h))
    conn.commit()
