import json
from pathlib import Path

from click.testing import CliRunner

from roastnet.cli import main

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

    first = runner.invoke(
        main, ["feed", "--feed-dir", str(feed_dir), "publish", str(FIXTURES_DIR / "kaleido_1.alog")]
    )
    assert first.exit_code == 0, first.output
    assert "entry 0" in first.output

    second = runner.invoke(
        main, ["feed", "--feed-dir", str(feed_dir), "publish", str(FIXTURES_DIR / "hottop_1.alog")]
    )
    assert second.exit_code == 0, second.output
    assert "entry 1" in second.output


def test_feed_verify_passes_on_own_feed(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    feed_dir = tmp_path / "feed"

    runner.invoke(main, ["feed", "--feed-dir", str(feed_dir), "publish", str(FIXTURES_DIR / "kaleido_1.alog")])
    result = runner.invoke(main, ["feed", "--feed-dir", str(feed_dir), "verify"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_feed_publish_then_ingest_shows_up_in_search(tmp_path: Path, monkeypatch) -> None:
    _isolate_home(monkeypatch, tmp_path)
    runner = CliRunner()
    feed_dir = tmp_path / "feed"
    db_path = tmp_path / "cli.sqlite3"

    runner.invoke(main, ["feed", "--feed-dir", str(feed_dir), "publish", str(FIXTURES_DIR / "kaleido_1.alog")])
    pubkey = runner.invoke(main, ["identity", "show"]).output.strip().splitlines()[-1]

    result = runner.invoke(
        main, ["--db", str(db_path), "feed", "--feed-dir", str(feed_dir), "ingest", "--pubkey", pubkey]
    )
    assert result.exit_code == 0, result.output
    assert "ingested 1" in result.output

    search_result = runner.invoke(main, ["--db", str(db_path), "search"])
    assert "no matches" not in search_result.output


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
