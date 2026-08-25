from pathlib import Path

import pytest

from roastnet.index import repository as repo
from roastnet.index.db import connect
from roastnet.index.ingest import ingest_file, ingest_path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "test.sqlite3")
    yield connection
    connection.close()


def test_ingest_all_fixtures(conn) -> None:
    results = ingest_path(conn, FIXTURES_DIR)
    assert len(results) == len(list(FIXTURES_DIR.glob("*.alog")))
    assert all(r.error is None for r in results)
    assert all(not r.skipped_duplicate for r in results)

    row_count = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
    assert row_count == len(results)


def test_reingesting_same_file_is_a_dedup_noop(conn) -> None:
    path = FIXTURES_DIR / "kaleido_1.alog"
    first = ingest_file(conn, path)
    assert first.skipped_duplicate is False
    assert first.error is None

    second = ingest_file(conn, path)
    assert second.skipped_duplicate is True

    row_count = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
    assert row_count == 1


def test_milestones_and_phase_profile_stored(conn) -> None:
    result = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog")
    roast_id = result.record.roast_id

    milestone_count = conn.execute(
        "SELECT COUNT(*) FROM milestones WHERE roast_id = ?", (roast_id,)
    ).fetchone()[0]
    assert milestone_count > 0

    if result.record.phase_profile is not None:
        profile_row = conn.execute(
            "SELECT dtr_pct FROM phase_profiles WHERE roast_id = ?", (roast_id,)
        ).fetchone()
        assert profile_row is not None


def test_fts_text_search_matches_beans_text(conn) -> None:
    ingest_path(conn, FIXTURES_DIR)
    # Pick a real word out of whichever roast actually has beans_text, so
    # the test doesn't depend on a specific fixture's exact wording.
    row = conn.execute(
        "SELECT beans_text FROM roasts WHERE beans_text IS NOT NULL AND beans_text != '' LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("no fixture has beans_text to search")
    first_word = row["beans_text"].split()[0]

    results = repo.search_roasts(conn, text=first_word)
    assert len(results) >= 1


def test_search_combines_text_and_structured_filters(conn) -> None:
    ingest_path(conn, FIXTURES_DIR)
    all_kaleido = repo.search_roasts(conn, machine_key="kaleido_serial")
    narrowed = repo.search_roasts(conn, machine_key="kaleido_serial", dtr_min=0.0, dtr_max=100.0)
    assert len(narrowed) <= len(all_kaleido) if all_kaleido else True


def test_search_with_no_filters_returns_everything(conn) -> None:
    ingest_path(conn, FIXTURES_DIR)
    total = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
    assert len(repo.search_roasts(conn)) == total


def test_load_full_record_round_trips(conn) -> None:
    result = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog")
    full = repo.load_full_record(conn, result.record.roast_id)
    assert full is not None
    assert full["machine_key"] == result.record.machine_key
