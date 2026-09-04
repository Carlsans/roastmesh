import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from roastmesh.cli import main

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_ingest_then_search(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()

    result = runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR)])
    assert result.exit_code == 0, result.output
    assert "ingested" in result.output

    result = runner.invoke(main, ["--db", str(db_path), "search"])
    assert result.exit_code == 0, result.output
    assert "no matches" not in result.output


def test_reindex_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()

    first = runner.invoke(main, ["--db", str(db_path), "reindex", str(FIXTURES_DIR)])
    assert first.exit_code == 0, first.output

    second = runner.invoke(main, ["--db", str(db_path), "reindex", str(FIXTURES_DIR)])
    assert second.exit_code == 0, second.output
    assert first.output == second.output


def test_search_with_machine_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR)])

    result = runner.invoke(main, ["--db", str(db_path), "search", "--machine", "hottop"])
    assert result.exit_code == 0, result.output


def test_ingest_single_file(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    result = runner.invoke(
        main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")]
    )
    assert result.exit_code == 0, result.output
    assert "ingested 1" in result.output


def _isolate_home(monkeypatch, tmp_path: Path) -> None:
    # identity/feed default paths live under Path.home(); isolate them per
    # test so this never touches (or depends on) a real user's config.
    monkeypatch.setenv("HOME", str(tmp_path))


def test_identity_show_creates_then_is_stable(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()

    first = runner.invoke(main, ["identity", "show"])
    assert first.exit_code == 0, first.output
    pubkey = first.output.strip().splitlines()[-1]
    assert len(pubkey) == 64

    second = runner.invoke(main, ["identity", "show"])
    assert second.exit_code == 0, second.output
    assert second.output.strip() == pubkey


def test_identity_export_prints_secret_key(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["identity", "export"])
    assert result.exit_code == 0, result.output
    assert len(result.output.strip()) == 64


def test_feed_publish_twice_appends_two_entries(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    feed_dir = tmp_path / "feed"
    db_path = tmp_path / "cli.sqlite3"

    first = runner.invoke(
        main, ["--db", str(db_path), "feed", "--feed-dir", str(feed_dir), "publish", str(FIXTURES_DIR / "kaleido_1.alog")]
    )
    assert first.exit_code == 0, first.output
    assert "entry 0" in first.output

    second = runner.invoke(
        main, ["--db", str(db_path), "feed", "--feed-dir", str(feed_dir), "publish", str(FIXTURES_DIR / "hottop_1.alog")]
    )
    assert second.exit_code == 0, second.output
    assert "entry 1" in second.output


def test_feed_verify_passes_on_own_feed(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    feed_dir = tmp_path / "feed"
    db_path = tmp_path / "cli.sqlite3"

    runner.invoke(main, ["--db", str(db_path), "feed", "--feed-dir", str(feed_dir), "publish", str(FIXTURES_DIR / "kaleido_1.alog")])
    result = runner.invoke(main, ["feed", "--feed-dir", str(feed_dir), "verify"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_feed_publish_shows_up_in_search_as_one_of_your_own_roasts(tmp_path: Path, monkeypatch) -> None:
    """Publishing alone must be enough -- there used to be no connection
    at all between `feed publish` and the local search index (publishing
    only ever touched the signed feed), so a person's own published
    roasts silently never showed up in their own search results unless
    they separately remembered to run `feed ingest` too."""
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    feed_dir = tmp_path / "feed"
    db_path = tmp_path / "cli.sqlite3"

    runner.invoke(main, ["--db", str(db_path), "feed", "--feed-dir", str(feed_dir), "publish", str(FIXTURES_DIR / "kaleido_1.alog")])

    search_result = runner.invoke(main, ["--db", str(db_path), "search", "--own-only"])
    assert "no matches" not in search_result.output


def test_feed_ingest_user_log_flag_marks_entries_as_your_own(tmp_path: Path, monkeypatch) -> None:
    """The backfill path -- `feed ingest --pubkey <yours> --user-log` --
    for a feed that predates the fix above and was never auto-ingested at
    publish time."""
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    feed_dir = tmp_path / "feed"
    db_path = tmp_path / "cli.sqlite3"

    # publish via append_entry directly (bypassing the CLI's own
    # auto-ingest-at-publish) to simulate a feed indexed by an old version
    from roastmesh.feed import append_entry
    from roastmesh.identity import load_or_create_identity
    identity, _ = load_or_create_identity()
    append_entry(feed_dir, identity, FIXTURES_DIR / "kaleido_1.alog", timestamp="2026-01-01T00:00:00Z")

    without_flag = runner.invoke(
        main, ["--db", str(db_path), "feed", "--feed-dir", str(feed_dir), "ingest", "--pubkey", identity.public_key_hex]
    )
    assert without_flag.exit_code == 0, without_flag.output
    assert "no matches" in runner.invoke(main, ["--db", str(db_path), "search", "--own-only"]).output

    with_flag = runner.invoke(
        main, ["--db", str(db_path), "feed", "--feed-dir", str(feed_dir), "ingest",
               "--pubkey", identity.public_key_hex, "--user-log"]
    )
    assert with_flag.exit_code == 0, with_flag.output
    assert "no matches" not in runner.invoke(main, ["--db", str(db_path), "search", "--own-only"]).output


def test_show_finds_a_roast_by_full_id_and_by_prefix(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")])
    roast_id = json.loads(
        runner.invoke(main, ["--db", str(db_path), "search", "--json"]).output
    )[0]["roast_id"]

    full = runner.invoke(main, ["--db", str(db_path), "show", roast_id])
    assert full.exit_code == 0, full.output
    assert "file:" in full.output
    assert str(FIXTURES_DIR / "kaleido_1.alog") in full.output

    prefix = runner.invoke(main, ["--db", str(db_path), "show", roast_id[:8]])
    assert prefix.exit_code == 0, prefix.output
    assert prefix.output == full.output


def test_show_annotates_roast_type_as_an_estimate(tmp_path: Path) -> None:
    """roast_type is always a peak-temperature estimate now -- a user was
    confused by an earlier version that instead trusted an explicit note
    in a file's own beans_text (a roast that peaked at 196C, unambiguously
    "light" on any standard chart, showed "full city+" because of an old
    note, with nothing indicating it wasn't a real bug). show should
    always make clear this is an estimate, not present it as a fact."""
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")])

    result = runner.invoke(main, ["--db", str(db_path), "search", "--json"])
    roast_id = json.loads(result.output)[0]["roast_id"]

    show_result = runner.invoke(main, ["--db", str(db_path), "show", roast_id])
    assert show_result.exit_code == 0, show_result.output
    assert "roast type:" in show_result.output
    assert "estimated from peak temperature" in show_result.output


def test_show_json_includes_record_and_raw_path(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")])
    roast_id = json.loads(
        runner.invoke(main, ["--db", str(db_path), "search", "--json"]).output
    )[0]["roast_id"]

    result = runner.invoke(main, ["--db", str(db_path), "show", roast_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["record"]["roast_id"] == roast_id
    assert payload["raw_path"] == str(FIXTURES_DIR / "kaleido_1.alog")


def test_show_reports_no_match_for_an_unknown_id(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")])

    result = runner.invoke(main, ["--db", str(db_path), "show", "0000000000"])
    assert result.exit_code != 0
    assert "no roast found" in result.output


def test_search_shows_all_peers_by_default_and_lan_only_narrows_it(tmp_path: Path) -> None:
    """Finding roasts from anywhere is the point of the network, so a plain
    `search` must include peers discovered over the internet DHT -- a user
    should not have to know a flag exists to see them. `--lan-only` is the
    opt-in that narrows results back to the local network."""
    from datetime import datetime, timezone

    from roastmesh.index.db import connect
    from roastmesh.index.ingest import ingest_file
    from roastmesh.peers import Peer, save_peers

    db_path = tmp_path / "cli.sqlite3"
    peers_file = tmp_path / "peers.json"
    lan_pubkey = "aa" * 32
    wan_pubkey = "bb" * 32

    conn = connect(db_path)
    ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref=f"{lan_pubkey}:00000000")
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog", source_type="p2p", source_ref=f"{wan_pubkey}:00000000")
    conn.close()

    now = datetime.now(timezone.utc).isoformat()
    save_peers([
        Peer(ticket="t1", feed_pubkey_hex=lan_pubkey, first_seen=now, last_seen=now, added_via="lan"),
        Peer(ticket="t2", feed_pubkey_hex=wan_pubkey, first_seen=now, last_seen=now, added_via="wan"),
    ], peers_file)

    runner = CliRunner()

    default_rows = json.loads(runner.invoke(
        main, ["--db", str(db_path), "search", "--peers-file", str(peers_file), "--json"]
    ).output)
    assert {row["source_ref"].split(":")[0] for row in default_rows} == {lan_pubkey, wan_pubkey}

    lan_rows = json.loads(runner.invoke(
        main, ["--db", str(db_path), "search", "--lan-only", "--peers-file", str(peers_file), "--json"]
    ).output)
    assert len(lan_rows) == 1
    assert lan_rows[0]["source_ref"].startswith(lan_pubkey)


def test_search_lan_only_keeps_own_local_roasts_regardless(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")])

    # no peers.json at all -- --lan-only must not touch source_type="local" rows
    result = runner.invoke(main, ["--db", str(db_path), "search"])
    assert "no matches" not in result.output


def test_hide_removes_a_roast_from_search_and_unhide_restores_it(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")])
    roast_id = json.loads(
        runner.invoke(main, ["--db", str(db_path), "search", "--json"]).output
    )[0]["roast_id"]

    hide_result = runner.invoke(main, ["--db", str(db_path), "hide", roast_id[:8]])
    assert hide_result.exit_code == 0, hide_result.output
    assert "hidden" in hide_result.output

    assert "no matches" in runner.invoke(main, ["--db", str(db_path), "search"]).output
    shown = runner.invoke(main, ["--db", str(db_path), "search", "--show-hidden"])
    assert "no matches" not in shown.output
    assert "[hidden]" in shown.output

    unhide_result = runner.invoke(main, ["--db", str(db_path), "unhide", roast_id[:8]])
    assert unhide_result.exit_code == 0, unhide_result.output
    assert "no matches" not in runner.invoke(main, ["--db", str(db_path), "search"]).output


def test_hide_reports_no_match_for_an_unknown_id(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")])

    result = runner.invoke(main, ["--db", str(db_path), "hide", "0000000000"])
    assert result.exit_code != 0
    assert "no roast found" in result.output


def test_show_json_includes_hidden_status(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")])
    roast_id = json.loads(
        runner.invoke(main, ["--db", str(db_path), "search", "--json"]).output
    )[0]["roast_id"]

    before = json.loads(runner.invoke(main, ["--db", str(db_path), "show", roast_id, "--json"]).output)
    assert before["hidden"] is False

    runner.invoke(main, ["--db", str(db_path), "hide", roast_id])
    after = json.loads(runner.invoke(main, ["--db", str(db_path), "show", roast_id, "--json"]).output)
    assert after["hidden"] is True
    assert "hidden: yes" in runner.invoke(main, ["--db", str(db_path), "show", roast_id]).output


# ---------------------------------------------------------------------------
# profile / user / machines (phase 2)
# ---------------------------------------------------------------------------

def test_profile_show_set_round_trip_including_custom_machine(tmp_path: Path, monkeypatch) -> None:
    from roastmesh.profile import verify_profile

    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["identity", "show"])  # create the identity quietly first --
    # otherwise its one-time "created new identity" banner (plain stdout,
    # not --json) would land ahead of the JSON this test parses below.

    default = json.loads(runner.invoke(main, ["profile", "show", "--json"]).output)
    assert default["name"]  # deterministic default name, never blank
    assert default["pubkey"]
    assert len(default["pubkey"]) == 64

    result = runner.invoke(main, ["profile", "set", "--name", "Amber Chaff", "--machine", "aillio_bullet"])
    assert result.exit_code == 0, result.output

    shown = json.loads(runner.invoke(main, ["profile", "show", "--json"]).output)
    assert shown["name"] == "Amber Chaff"
    assert shown["machine_key"] == "aillio_bullet"
    assert shown["machine_display"] == "Aillio Bullet R1"
    assert verify_profile(shown) is True

    # a custom machine not in the catalogue
    custom = runner.invoke(main, ["profile", "set", "--machine-custom", "My Home-Built Drum Roaster"])
    assert custom.exit_code == 0, custom.output

    shown2 = json.loads(runner.invoke(main, ["profile", "show", "--json"]).output)
    assert shown2["machine_display"] == "My Home-Built Drum Roaster"
    assert shown2["machine_key"] == "my_home_built_drum_roaster"
    assert shown2["name"] == "Amber Chaff"  # untouched by the machine-only update
    assert verify_profile(shown2) is True


def test_profile_set_rejects_unknown_machine_key(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["profile", "set", "--machine", "not_a_real_machine_key"])
    assert result.exit_code != 0
    assert "unknown machine" in result.output


def test_profile_set_rejects_machine_and_machine_custom_together(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main, ["profile", "set", "--machine", "aillio_bullet", "--machine-custom", "Home Rig"]
    )
    assert result.exit_code != 0


def test_user_list_with_roasts_default_vs_all_and_default_name_fallback(tmp_path: Path) -> None:
    from roastmesh.index import repository as repo
    from roastmesh.index.db import connect
    from roastmesh.index.ingest import ingest_file
    from roastmesh.usernames import default_display_name

    db_path = tmp_path / "cli.sqlite3"
    publisher_pubkey = "aa" * 32
    favorited_no_roasts_pubkey = "bb" * 32

    conn = connect(db_path)
    ingest_file(
        conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref=f"{publisher_pubkey}:00000000"
    )
    repo.ensure_user(conn, favorited_no_roasts_pubkey)
    repo.set_user_favorite(conn, favorited_no_roasts_pubkey, True)
    conn.close()

    runner = CliRunner()
    with_roasts = json.loads(runner.invoke(main, ["--db", str(db_path), "user", "list", "--json"]).output)
    pubkeys_with_roasts = {u["pubkey_hex"] for u in with_roasts}
    assert publisher_pubkey in pubkeys_with_roasts
    assert favorited_no_roasts_pubkey not in pubkeys_with_roasts  # has no roasts -- hidden by default

    all_users = json.loads(runner.invoke(main, ["--db", str(db_path), "user", "list", "--all", "--json"]).output)
    pubkeys_all = {u["pubkey_hex"] for u in all_users}
    assert publisher_pubkey in pubkeys_all
    assert favorited_no_roasts_pubkey in pubkeys_all  # --all surfaces it too

    publisher_row = next(u for u in with_roasts if u["pubkey_hex"] == publisher_pubkey)
    assert publisher_row["display_name"] == default_display_name(publisher_pubkey)  # never-synced fallback
    assert publisher_row["roast_count"] == 1


def test_user_favorite_and_unfavorite(tmp_path: Path) -> None:
    from roastmesh.index.db import connect
    from roastmesh.index.ingest import ingest_file

    db_path = tmp_path / "cli.sqlite3"
    pubkey = "cc" * 32
    conn = connect(db_path)
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog", source_type="p2p", source_ref=f"{pubkey}:00000000")
    conn.close()

    runner = CliRunner()
    fav_result = runner.invoke(main, ["--db", str(db_path), "user", "favorite", pubkey[:8]])
    assert fav_result.exit_code == 0, fav_result.output

    shown = json.loads(runner.invoke(main, ["--db", str(db_path), "user", "show", pubkey[:8], "--json"]).output)
    assert shown["is_favorite"] is True

    unfav_result = runner.invoke(main, ["--db", str(db_path), "user", "unfavorite", pubkey[:8]])
    assert unfav_result.exit_code == 0, unfav_result.output
    shown2 = json.loads(runner.invoke(main, ["--db", str(db_path), "user", "show", pubkey[:8], "--json"]).output)
    assert shown2["is_favorite"] is False


def test_user_show_reports_no_match_for_an_unknown_id(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    result = runner.invoke(main, ["--db", str(db_path), "user", "show", "00000000"])
    assert result.exit_code != 0
    assert "no user found" in result.output


def test_user_like_and_unlike_resign_profile_and_update_like_count(tmp_path: Path, monkeypatch) -> None:
    from roastmesh.index.db import connect
    from roastmesh.index.ingest import ingest_file
    from roastmesh.profile import default_profile_path, load_profile, verify_profile

    _isolate_home(monkeypatch, tmp_path)
    db_path = tmp_path / "cli.sqlite3"
    subject_pubkey = "dd" * 32
    conn = connect(db_path)
    ingest_file(conn, FIXTURES_DIR / "kaleido_2.alog", source_type="p2p", source_ref=f"{subject_pubkey}:00000000")
    conn.close()

    runner = CliRunner()
    like_result = runner.invoke(main, ["--db", str(db_path), "user", "like", subject_pubkey[:8]])
    assert like_result.exit_code == 0, like_result.output

    saved = load_profile(default_profile_path())
    assert saved is not None
    assert subject_pubkey in saved.likes
    assert verify_profile(saved.to_dict()) is True  # re-signed, still verifies

    shown = json.loads(
        runner.invoke(main, ["--db", str(db_path), "user", "show", subject_pubkey[:8], "--json"]).output
    )
    assert shown["like_count"] == 1

    unlike_result = runner.invoke(main, ["--db", str(db_path), "user", "unlike", subject_pubkey[:8]])
    assert unlike_result.exit_code == 0, unlike_result.output

    saved2 = load_profile(default_profile_path())
    assert saved2 is not None
    assert subject_pubkey not in saved2.likes
    assert verify_profile(saved2.to_dict()) is True

    shown2 = json.loads(
        runner.invoke(main, ["--db", str(db_path), "user", "show", subject_pubkey[:8], "--json"]).output
    )
    assert shown2["like_count"] == 0


def test_search_user_and_favorites_only_filters(tmp_path: Path) -> None:
    from roastmesh.index.db import connect
    from roastmesh.index.ingest import ingest_file

    db_path = tmp_path / "cli.sqlite3"
    pubkey_a = "ee" * 32
    pubkey_b = "ff" * 32
    conn = connect(db_path)
    ingest_file(conn, FIXTURES_DIR / "kaleido_3.alog", source_type="p2p", source_ref=f"{pubkey_a}:00000000")
    ingest_file(conn, FIXTURES_DIR / "hottop_2.alog", source_type="p2p", source_ref=f"{pubkey_b}:00000000")
    conn.close()

    runner = CliRunner()
    user_rows = json.loads(
        runner.invoke(main, ["--db", str(db_path), "search", "--user", pubkey_a[:8], "--json"]).output
    )
    assert len(user_rows) == 1
    assert user_rows[0]["author_pubkey"] == pubkey_a

    runner.invoke(main, ["--db", str(db_path), "user", "favorite", pubkey_b[:8]])
    fav_rows = json.loads(
        runner.invoke(main, ["--db", str(db_path), "search", "--favorites-only", "--json"]).output
    )
    assert {r["author_pubkey"] for r in fav_rows} == {pubkey_b}


def test_machines_list_and_used(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()

    catalogue = json.loads(runner.invoke(main, ["--db", str(db_path), "machines", "list", "--json"]).output)
    assert any(m["key"] == "aillio_bullet" for m in catalogue)

    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR)])
    used = json.loads(
        runner.invoke(main, ["--db", str(db_path), "machines", "list", "--used", "--json"]).output
    )
    assert isinstance(used, list)
    assert len(used) > 0


def test_refresh_updates_stale_entries_and_is_a_fast_noop_second_time(tmp_path: Path) -> None:
    import roastmesh

    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")])

    from roastmesh.index.db import connect
    conn = connect(db_path)
    conn.execute("UPDATE roasts SET title = NULL")  # simulate a pre-title-field entry
    conn.commit()
    conn.close()

    first = runner.invoke(main, ["--db", str(db_path), "refresh"])
    assert first.exit_code == 0, first.output
    assert f"refreshed 1 roast(s) for v{roastmesh.__version__}" in first.output

    conn = connect(db_path)
    title = conn.execute("SELECT title FROM roasts").fetchone()["title"]
    conn.close()
    assert title is not None

    second = runner.invoke(main, ["--db", str(db_path), "refresh"])
    assert second.exit_code == 0, second.output
    assert "already up to date" in second.output


def test_refresh_force_reruns_even_when_already_up_to_date(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")])
    runner.invoke(main, ["--db", str(db_path), "refresh"])

    forced = runner.invoke(main, ["--db", str(db_path), "refresh", "--force"])
    assert forced.exit_code == 0, forced.output
    assert "refreshed 1 roast(s)" in forced.output


def test_refresh_preserves_hidden_status(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.sqlite3"
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "ingest", str(FIXTURES_DIR / "kaleido_1.alog")])
    roast_id = json.loads(
        runner.invoke(main, ["--db", str(db_path), "search", "--json"]).output
    )[0]["roast_id"]
    runner.invoke(main, ["--db", str(db_path), "hide", roast_id])

    runner.invoke(main, ["--db", str(db_path), "refresh", "--force"])

    payload = json.loads(runner.invoke(main, ["--db", str(db_path), "show", roast_id, "--json"]).output)
    assert payload["hidden"] is True


def test_declaring_your_machine_makes_your_blank_roastertype_roasts_findable(tmp_path: Path, monkeypatch) -> None:
    """The owner-machine fallback, end to end through the CLI.

    `search --machine X` matches a roast whose own machine_key is X, OR one
    whose machine is 'unknown' but whose owner declared X -- which is what
    makes the facet useful at all, since 5 of the 9 sample .alog files carry
    a blank `roastertype`.

    Regression: this was dead on arrival. `profile set` wrote profile.json but
    never mirrored the declared machine into the index's `users` table, and
    the fallback is a SQL join onto that table -- so declaring a machine and
    then searching for it returned "no matches". Each phase's unit tests
    exercised one side of that join and neither crossed it.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    db_path = tmp_path / "index.sqlite3"
    runner = CliRunner()

    # philstyle_1.alog has roastertype == "" -> machine_key "unknown".
    ingested = runner.invoke(main, ["--db", str(db_path), "ingest",
                                    str(FIXTURES_DIR / "philstyle_1.alog")])
    assert ingested.exit_code == 0, ingested.output

    assert runner.invoke(main, ["--db", str(db_path), "search", "--machine", "hottop"]
                         ).output.strip() == "no matches"

    declared = runner.invoke(main, ["--db", str(db_path), "profile", "set", "--machine", "hottop"])
    assert declared.exit_code == 0, declared.output

    found = runner.invoke(main, ["--db", str(db_path), "search", "--machine", "hottop", "--json"])
    assert found.exit_code == 0, found.output
    rows = json.loads(found.output)
    assert len(rows) == 1, rows
    assert rows[0]["machine_key"] == "unknown"  # matched via its owner, not its own key

    # A machine nobody declared and no roast carries must still match nothing.
    other = runner.invoke(main, ["--db", str(db_path), "search", "--machine", "giesen_w6a", "--json"])
    assert json.loads(other.output) == []


def test_json_output_is_pure_json_even_on_the_run_that_creates_the_identity(
        tmp_path: Path, monkeypatch) -> None:
    """`--json` must print only JSON on stdout.

    The first run creates an Ed25519 identity and says so. That notice used to
    go to stdout, so whichever `--json` command happened to be the one
    creating the identity returned "created new identity: ...\\n{...}" --
    not JSON. The GUI parsed it, threw, and silently showed a blank field to
    every brand-new user.
    """
    home = tmp_path / "home"
    home.mkdir()
    # HOME alone does not isolate anything on Windows -- Path.home() resolves
    # USERPROFILE there, so this test ran against the real profile, found an
    # identity already present, and saw no first-run notice at all. Caught by
    # the Windows CI job, which is exactly what it is for.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    db_path = tmp_path / "index.sqlite3"

    result = CliRunner().invoke(
        main, ["--db", str(db_path), "profile", "show", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)   # must not need any cleaning up
    assert len(payload["pubkey"]) == 64
    assert payload["name"]

    # the notice still has to reach the user -- on stderr, where it belongs
    assert "created new identity" in result.stderr


# --- node doctor's rendering ------------------------------------------------

def _doctor_report(**over) -> dict:
    report = {
        "identity": "ab" * 32, "node_id": "cd" * 20, "node_id_bep42": True,
        "state_path": "/tmp/dht_state.json", "state_nodes": 12,
        "bootstrap": [{"host": "dht.transmissionbt.com", "addr": "87.98.162.88:6881",
                       "ok": True}],
        "bootstrap_unresolved": 0, "announced_this_run": True,
        "external_ip": "209.227.189.65", "external_port": 48973,
        "nat": "consistent", "ip_votes": 17,
        "routing_table": {"total": 40, "good": 30, "verified": 21}, "warm": True,
        "lookup": {"rounds": 14, "queried": 47, "replied": 15, "closest_bits": 140,
                   "announced": 8, "no_token": 0, "peers_found": 1,
                   "rejected_martian": 0, "rejected_impossible_proximity": 0,
                   "rejected_bep42": 29},
        "announce_set": [{"addr": "1.2.3.4:6881", "bits": 140, "bep42": True}],
        "readback": True, "peers": ["5.6.7.8:41890"],
        "public_port": None, "needs_public_port": False,
        "router_external_ip": None, "double_nat": None,
        "swarm_info_hash": "22" * 20,
    }
    report.update(over)
    return report


def _render(report: dict) -> str:
    from roastmesh.cli import _print_doctor_report

    runner = CliRunner()
    with runner.isolation() as out:
        _print_doctor_report(report)
        return out[0].getvalue().decode()


def test_doctor_reports_a_healthy_node_without_warnings() -> None:
    text = _render(_doctor_report())
    assert "209.227.189.65:48973" in text
    assert "consistent mapping" in text
    assert "read-back: OK" in text
    assert "WARNING" not in text


def test_doctor_calls_out_a_symmetric_nat_as_a_network_limit_not_a_dht_fault() -> None:
    """The single most useful thing this command can tell a user who has
    "connected but finds nobody": no amount of DHT correctness will help,
    because nothing can send them a first packet."""
    text = _render(_doctor_report(nat="symmetric"))
    assert "carrier-grade NAT" in text
    assert "not a DHT fault" in text


def test_doctor_warns_when_the_announce_set_contains_an_unverified_node() -> None:
    text = _render(_doctor_report(
        announce_set=[{"addr": "1.2.3.4:6881", "bits": 40, "bep42": False}]))
    assert "UNVERIFIED" in text
    assert "WARNING" in text


def test_doctor_warns_when_the_lookup_reaches_an_impossible_distance() -> None:
    """A lookup that lands closer than any honest node can be is the exact
    signature of the sybil capture this release fixes -- if it ever shows up
    again, the filters have been defeated and the report must say so rather
    than presenting a suspiciously excellent convergence as good news."""
    report = _doctor_report()
    report["lookup"]["closest_bits"] = 38
    text = _render(report)
    assert "no honest node can occupy" in text


def test_doctor_reports_a_failed_read_back_as_a_failure() -> None:
    text = _render(_doctor_report(readback=False))
    assert "read-back: FAILED" in text
    assert "will not find us" in text


def test_peer_sync_reports_an_unreachable_peer_instead_of_crashing(tmp_path, monkeypatch) -> None:
    """An unreachable peer is an ordinary outcome, not a crash.

    Left unhandled this printed a PyInstaller traceback ending in a bare
    `iroh.iroh_ffi.IrohError` -- seen for real on a Raspberry Pi whose DNS was
    dead, so iroh could not resolve a relay to connect through. Nothing in that
    output told the user what had happened or what to do next.
    """
    from roastmesh import cli as cli_mod

    class _IrohishError(Exception):
        pass

    async def _boom(*_a, **_kw):
        raise _IrohishError("connection timed out")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Path.home() on Windows
    monkeypatch.setattr(cli_mod.net, "sync_with_peer", _boom)

    result = CliRunner().invoke(main, ["peer", "sync", "endpointabcdef"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "could not connect to that peer" in result.output
    assert "_IrohishError" in result.output
    assert "check DNS first" in result.output


def test_doctor_does_not_promise_reachability_from_a_stable_mapping() -> None:
    """A consistent NAT mapping says the address is stable. It says nothing
    about whether the router accepts a stranger's first packet -- measured:
    the machine this was developed on reports a stable mapping and silently
    drops unsolicited datagrams, so a peer that found it still could not
    reach it. Claiming "other nodes can reach this port" sent the diagnosis
    in the wrong direction."""
    text = _render(_doctor_report())
    assert "other nodes can reach this port" not in text
    assert "filtering" in text


def test_doctor_tells_a_symmetric_nat_exactly_what_to_do() -> None:
    """"Not findable" on its own leaves the user nowhere. The report has to
    name the fix, and the command that applies it."""
    text = _render(_doctor_report(nat="symmetric", readback=False,
                                  needs_public_port=True))
    assert "needs a forwarded port" in text
    assert "--public-port N" in text
    assert "piactl get portforward" in text          # the VPN route out of CGNAT
    assert "can still find others and sync" in text  # what still works meanwhile


def test_doctor_does_not_nag_a_node_whose_forwarded_port_works() -> None:
    text = _render(_doctor_report(public_port=26513, readback=True,
                                  needs_public_port=False))
    assert "needs a forwarded port" not in text


def test_doctor_says_a_configured_port_is_not_actually_open() -> None:
    """Configured but still unreachable is a different problem from having no
    port at all, and repeating the generic advice would send the user back to
    a step they already did."""
    text = _render(_doctor_report(nat="symmetric", public_port=26513,
                                  readback=False, needs_public_port=True))
    assert "26513 is configured but the read-back still failed" in text
    assert "--public-port N" not in text


def test_public_port_accepts_a_number_or_auto_and_rejects_anything_else() -> None:
    """Rejected loudly rather than ignored. A typo here means publishing the
    wrong port or none at all, and the only symptom is that nobody ever
    arrives -- the least diagnosable failure this program has."""
    from roastmesh.cli import _parse_public_port

    assert _parse_public_port(None) == (False, None)
    assert _parse_public_port("26513") == (False, 26513)
    assert _parse_public_port(" AUTO ") == (True, None)

    for bad in ("bogus", "0", "70000", "-1", "26513x"):
        with pytest.raises(click.BadParameter):
            _parse_public_port(bad)


def test_doctor_names_double_nat_when_the_router_admits_to_it() -> None:
    """The one diagnosis nothing else can make.

    A router reporting a private address as its own public one is behind the
    ISP's NAT as well, and no port forwarded on it can be reached. Saying so
    stops a user spending an evening on port-forwarding settings that cannot
    possibly work.
    """
    text = _render(_doctor_report(router_external_ip="100.64.0.1", double_nat="double-nat"))
    assert "behind another NAT" in text
    assert "carrier-grade" in text.lower()


def test_double_nat_replaces_the_forward_a_port_advice_rather_than_adding_to_it() -> None:
    """Telling someone to forward a port *and* that forwarding cannot work
    would be worse than saying nothing."""
    text = _render(_doctor_report(nat="symmetric", readback=False, needs_public_port=True,
                                  router_external_ip="100.64.0.1", double_nat="double-nat"))
    assert "nothing here will help" in text
    assert "--public-port N" not in text
    assert "VPN that offers port forwarding" in text


def test_a_router_that_agrees_is_reported_as_confirmation() -> None:
    text = _render(_doctor_report(router_external_ip="209.227.189.65", double_nat="agrees"))
    assert "router agrees" in text
    assert "WARNING" not in text


def test_a_disagreement_is_surfaced_without_picking_a_side() -> None:
    text = _render(_doctor_report(router_external_ip="198.51.100.1", double_nat="disagrees"))
    assert "198.51.100.1" in text and "209.227.189.65" in text
    # Observed for real: the ISP changed the address, the router knew at once
    # and the DHT tally was still carrying the old one. Reporting that as "more
    # than one route out" would have sent the user looking for a second WAN
    # link that does not exist.
    assert "has not caught up" in text
    assert "route out" not in text


def test_doctor_does_not_claim_agreement_with_nothing() -> None:
    text = _render(_doctor_report(external_ip=None, router_external_ip="216.209.221.161",
                                  double_nat="unconfirmed"))
    assert "216.209.221.161" in text
    assert "nothing to compare" in text
    assert "agrees" not in text


def test_doctor_offers_both_explanations_for_a_disagreement() -> None:
    """A VPN and a changed address produce the same symptom, and only one of
    them is fixed by forwarding a port."""
    text = _render(_doctor_report(router_external_ip="216.209.221.161", double_nat="disagrees"))
    assert "VPN" in text
    assert "not caught up" in text


# --- device pairing / private folder sync -----------------------------------

def _seed_device(pubkey: str, name: str = "Carl's Pi") -> None:
    from roastmesh.devices import Device, add_device
    add_device(Device(pubkey=pubkey, name=name, platform="linux", paired_at="2026-01-01T00:00:00+00:00"))


def test_device_list_json_empty_shape(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["device", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_device_list_text_with_no_paired_devices(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["device", "list"])
    assert result.exit_code == 0, result.output
    assert "no paired devices" in result.output


def test_device_list_json_reports_a_seeded_device_as_not_online_without_a_real_probe(
    tmp_path: Path, monkeypatch,
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    pubkey = "a" * 64
    _seed_device(pubkey, "Carl's Pi")
    runner = CliRunner()
    # --no-probe: no real network listen, so this is safe and instant under test.
    result = runner.invoke(main, ["device", "list", "--json", "--no-probe"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["pubkey"] == pubkey
    assert rows[0]["name"] == "Carl's Pi"
    assert rows[0]["online"] is False


def test_device_remove_a_seeded_device(tmp_path: Path, monkeypatch) -> None:
    from roastmesh.devices import load_devices

    _isolate_home(monkeypatch, tmp_path)
    pubkey = "b" * 64
    _seed_device(pubkey, "Carl's Pi")

    runner = CliRunner()
    result = runner.invoke(main, ["device", "remove", pubkey])
    assert result.exit_code == 0, result.output
    assert "removed" in result.output
    assert "Carl's Pi" in result.output
    assert load_devices() == []


def test_device_remove_by_name(tmp_path: Path, monkeypatch) -> None:
    from roastmesh.devices import load_devices

    _isolate_home(monkeypatch, tmp_path)
    _seed_device("c" * 64, "Kitchen Pi")

    runner = CliRunner()
    result = runner.invoke(main, ["device", "remove", "Kitchen Pi"])
    assert result.exit_code == 0, result.output
    assert load_devices() == []


def test_device_remove_reports_no_match_for_an_unknown_device(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["device", "remove", "d" * 64])
    assert result.exit_code != 0
    assert "no paired device" in result.output


def test_device_pair_json_emits_the_expected_event_shape(tmp_path: Path, monkeypatch) -> None:
    """pair_over_lan itself does real network I/O (device_sync.py) -- monkeypatched
    here so this test exercises only the CLI's event emission and stdin-driven
    confirm/pick plumbing, with the match answer ('y') fed through stdin the
    same way the real `--json` contract expects."""
    from roastmesh import cli as cli_mod
    from roastmesh.lan_discovery import PairingCandidate
    from roastmesh.pairing import PairResult

    _isolate_home(monkeypatch, tmp_path)

    async def fake_pair_over_lan(identity, *, confirm, timeout, on_status, name):
        candidates = [PairingCandidate(pubkey="a" * 64, ticket="t", code="4821", hostname="other-host")]
        picked = on_status(candidates)  # exactly one candidate -> no stdin read here
        assert picked is None
        sas = [("🐶", "Dog")] * 7
        matched = confirm(sas)  # this is what reads the 'y' from stdin in --json mode
        return PairResult(ok=bool(matched), remote_pubkey_hex=("a" * 64) if matched else None, sas=sas)

    monkeypatch.setattr(cli_mod.device_sync, "pair_over_lan", fake_pair_over_lan)

    runner = CliRunner()
    result = runner.invoke(main, ["device", "pair", "--json"], input="y\n")
    assert result.exit_code == 0, result.output

    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [e["event"] for e in events] == ["discovered", "sas", "result"]
    assert events[0]["devices"] == [{"pubkey": "a" * 64, "code": "4821", "hostname": "other-host"}]
    assert events[1]["emoji"][0] == ["🐶", "Dog"]
    assert events[2] == {"event": "result", "ok": True, "pubkey": "a" * 64, "name": None}


def test_device_pair_json_reports_a_declined_match(tmp_path: Path, monkeypatch) -> None:
    from roastmesh import cli as cli_mod
    from roastmesh.lan_discovery import PairingCandidate
    from roastmesh.pairing import PairResult

    _isolate_home(monkeypatch, tmp_path)

    async def fake_pair_over_lan(identity, *, confirm, timeout, on_status, name):
        on_status([PairingCandidate(pubkey="a" * 64, ticket="t", code="1", hostname="h")])
        matched = confirm([("🐶", "Dog")] * 7)
        return PairResult(ok=bool(matched), remote_pubkey_hex=None, sas=[("🐶", "Dog")] * 7,
                          error="not confirmed on this device")

    monkeypatch.setattr(cli_mod.device_sync, "pair_over_lan", fake_pair_over_lan)

    runner = CliRunner()
    result = runner.invoke(main, ["device", "pair", "--json"], input="n\n")
    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert events[-1]["ok"] is False
