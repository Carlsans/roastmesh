from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from roastnet.alog.machine import normalize_machine_key
from roastnet.models import RoastRecord


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_source_by_hash(conn: sqlite3.Connection, content_sha256: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM sources WHERE content_sha256 = ?", (content_sha256,))
    return cur.fetchone()


def insert_source(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    source_type: str,
    source_ref: str,
    source_url: str | None,
    raw_path: str,
    content_sha256: str,
) -> None:
    conn.execute(
        """INSERT INTO sources (source_id, source_type, source_ref, source_url,
                                 fetched_at, raw_path, content_sha256)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (source_id, source_type, source_ref, source_url,
         datetime.now(timezone.utc).isoformat(), raw_path, content_sha256),
    )


def insert_roast(conn: sqlite3.Connection, record: RoastRecord, source_id: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO roasts
           (roast_id, source_id, roast_uuid, roaster_type_raw, machine_key, mechanism_family,
            batch_weight_in_g, batch_weight_out_g, density_g_per_l, beans_text, roast_date,
            roast_epoch, roast_type, roasting_notes, cupping_notes, is_user_log,
            parse_warnings_json, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record.roast_id, source_id, record.roast_uuid, record.roaster_type_raw,
            record.machine_key, record.mechanism_family, record.batch_weight_in_g,
            record.batch_weight_out_g, record.density_g_per_l, record.beans_text,
            record.roast_date, record.roast_epoch, record.roast_type, record.roasting_notes,
            record.cupping_notes, int(record.is_user_log),
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
        """INSERT INTO roasts_fts (roast_id, beans_text, roasting_notes, cupping_notes,
                                    roast_type, machine_display)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (record.roast_id, record.beans_text, record.roasting_notes, record.cupping_notes,
         record.roast_type, machine_display),
    )


@dataclass
class RoastSearchRow:
    roast_id: str
    machine_key: str
    mechanism_family: str
    roast_type: str | None
    batch_weight_in_g: float | None
    density_g_per_l: float | None
    beans_text: str | None
    roast_date: str | None
    dtr_pct: float | None
    total_time_s: float | None
    drop_bt_c: float | None
    source_ref: str


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
) -> list[RoastSearchRow]:
    sql = """
        SELECT r.roast_id, r.machine_key, r.mechanism_family, r.roast_type,
               r.batch_weight_in_g, r.density_g_per_l, r.beans_text, r.roast_date,
               s.source_ref, p.dtr_pct, p.total_time_s,
               (SELECT bt_c FROM milestones m WHERE m.roast_id = r.roast_id AND m.name = 'DROP') AS drop_bt_c,
               (SELECT bt_c FROM milestones m WHERE m.roast_id = r.roast_id AND m.name = 'SC_START') AS sc_start_bt_c
        FROM roasts r
        JOIN sources s ON s.source_id = r.source_id
        LEFT JOIN phase_profiles p ON p.roast_id = r.roast_id
    """
    conditions: list[str] = []
    params: list = []

    if text:
        sql += " JOIN roasts_fts f ON f.roast_id = r.roast_id"
        conditions.append("roasts_fts MATCH ?")
        params.append(_fts_query(text))
    if machine_key:
        conditions.append("r.machine_key = ?")
        params.append(machine_key)
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
            beans_text=row["beans_text"],
            roast_date=row["roast_date"],
            dtr_pct=row["dtr_pct"],
            total_time_s=row["total_time_s"],
            drop_bt_c=row["drop_bt_c"],
            source_ref=row["source_ref"],
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
