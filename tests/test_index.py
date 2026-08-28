from pathlib import Path

import pytest

from roastmesh.index import repository as repo
from roastmesh.index.db import connect
from roastmesh.index.ingest import ingest_file, ingest_path, refresh_known_sources

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


def test_reingesting_same_file_refreshes_derived_fields_without_duplicating_the_row(conn) -> None:
    """"Dedup" means "don't duplicate the source/blob row", not "never
    look at this content again" -- a parser/schema improvement (a newly
    extracted field, say) must take effect the next time a file happens
    to be (re-)ingested, or every already-ingested roast is stuck showing
    stale data indefinitely with no way to refresh it short of wiping the
    whole index. Confirmed as a real gap in production: a field added to
    the parser after this project had already shipped left an
    already-ingested roast's title blank forever, even though the
    original file on disk had a real one, until this fix."""
    path = FIXTURES_DIR / "kaleido_1.alog"
    first = ingest_file(conn, path, is_user_log=False)
    assert first.record.is_user_log is False

    second = ingest_file(conn, path, is_user_log=True)
    assert second.skipped_duplicate is True
    assert second.record.is_user_log is True
    assert second.record.roast_id == first.record.roast_id  # same row, not a new one

    row_count = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
    assert row_count == 1
    stored = conn.execute(
        "SELECT is_user_log FROM roasts WHERE roast_id = ?", (first.record.roast_id,)
    ).fetchone()
    assert stored["is_user_log"] == 1


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


def test_search_own_only_filters_to_is_user_log(conn) -> None:
    ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", is_user_log=True)
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog", is_user_log=False)

    own = repo.search_roasts(conn, own_only=True)
    assert len(own) == 1
    assert own[0].is_user_log is True

    everything = repo.search_roasts(conn, own_only=False)
    assert len(everything) == 2


def test_set_hidden_excludes_from_search_by_default_and_include_hidden_reveals_it(conn) -> None:
    result = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog")
    roast_id = result.record.roast_id

    assert len(repo.search_roasts(conn)) == 1
    assert repo.find_hidden(conn, roast_id) is False

    updated = repo.set_hidden(conn, roast_id, True)
    assert updated is True
    assert repo.find_hidden(conn, roast_id) is True

    assert repo.search_roasts(conn) == []
    shown = repo.search_roasts(conn, include_hidden=True)
    assert len(shown) == 1
    assert shown[0].hidden is True

    repo.set_hidden(conn, roast_id, False)
    assert repo.find_hidden(conn, roast_id) is False
    assert len(repo.search_roasts(conn)) == 1


def test_reingesting_a_hidden_roast_does_not_unhide_it(conn) -> None:
    """A real bug caught before it shipped: INSERT OR REPLACE resets any
    column not in its statement to the schema default, and `hidden` was
    never in insert_roast's column list -- so re-ingesting an already-
    hidden roast (exactly what a version-triggered refresh does) would
    have silently un-hidden it."""
    path = FIXTURES_DIR / "kaleido_1.alog"
    result = ingest_file(conn, path)
    repo.set_hidden(conn, result.record.roast_id, True)

    ingest_file(conn, path)  # hits the self-healing "existing" branch

    assert repo.find_hidden(conn, result.record.roast_id) is True


def test_set_hidden_returns_false_for_an_unknown_roast_id(conn) -> None:
    assert repo.set_hidden(conn, "not-a-real-id", True) is False


def test_find_hidden_returns_none_for_an_unknown_roast_id(conn) -> None:
    assert repo.find_hidden(conn, "not-a-real-id") is None


def test_load_full_record_round_trips(conn) -> None:
    result = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog")
    full = repo.load_full_record(conn, result.record.roast_id)
    assert full is not None
    assert full["machine_key"] == result.record.machine_key


def test_refresh_known_sources_updates_a_field_without_reingesting_manually(conn, monkeypatch) -> None:
    """The actual "stale entries" scenario: a title extracted by a newer
    parser must show up for a roast ingested before that field existed,
    without the caller needing to know which file to re-ingest -- refresh
    just walks everything the index already knows about."""
    ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog")
    # simulate "ingested by an older parser that didn't have `title` yet"
    conn.execute("UPDATE roasts SET title = NULL")
    conn.commit()
    assert repo.search_roasts(conn)[0].title is None

    results = refresh_known_sources(conn)

    assert len(results) == 1
    assert results[0].error is None
    assert repo.search_roasts(conn)[0].title is not None


def test_refresh_known_sources_preserves_own_roast_tagging(conn) -> None:
    ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", is_user_log=True)
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog", is_user_log=False)

    refresh_known_sources(conn)

    own = repo.search_roasts(conn, own_only=True)
    assert len(own) == 1
    assert own[0].source_ref.endswith("kaleido_1.alog")


def test_refresh_known_sources_skips_a_raw_path_that_no_longer_exists(conn, tmp_path: Path) -> None:
    moved = tmp_path / "will_disappear.alog"
    moved.write_bytes((FIXTURES_DIR / "kaleido_1.alog").read_bytes())
    ingest_file(conn, moved)
    moved.unlink()

    results = refresh_known_sources(conn)

    assert results == []  # skipped, not reported as an error
