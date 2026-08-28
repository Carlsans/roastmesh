import json
from pathlib import Path

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
