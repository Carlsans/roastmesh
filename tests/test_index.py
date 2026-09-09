from pathlib import Path

import pytest

from roastmesh.feed import append_entry
from roastmesh.identity import generate_identity
from roastmesh.index import repository as repo
from roastmesh.index.db import connect
from roastmesh.index.ingest import (
    apply_delivered_edit, edit_unpublished_roast_notes, ingest_feed, ingest_file, ingest_path,
    refresh_known_sources,
)

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


def test_refresh_rewrites_a_machine_key_left_by_an_older_catalogue(conn) -> None:
    """This is the whole migration path for the catalogue rework, so it is
    worth proving rather than assuming.

    Entries indexed before that change carry keys derived by slugifying the
    Artisan string, so a Coffed SR5 sits under `coffed_sr5_manual_delta` and
    a Hottop's display reads "Hottop 2K+". Nothing hand-migrates those --
    `roaster_type_raw` is kept on every row, so the version-gated refresh
    that already runs on startup re-derives both fields from the original
    file. If that stopped working, existing users would silently keep the
    old buckets and see none of this.
    """
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog")
    conn.execute("UPDATE roasts SET machine_key = 'hottop_2k'")
    conn.commit()
    assert not repo.search_roasts(conn, machine_key="hottop")

    refresh_known_sources(conn)

    row = conn.execute("SELECT machine_key, roaster_type_raw FROM roasts").fetchone()
    assert row["roaster_type_raw"] == "Hottop 2K+", "the raw string must survive untouched"
    assert row["machine_key"] == "hottop"
    assert repo.search_roasts(conn, machine_key="hottop")


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


# ---------------------------------------------------------------------------
# author_pubkey: population at ingest time, and backfill via
# refresh_known_sources for both p2p and local rows.
# ---------------------------------------------------------------------------

def _author_pubkey_for(conn, roast_id: str) -> str | None:
    row = conn.execute(
        "SELECT s.author_pubkey FROM roasts r JOIN sources s ON s.source_id = r.source_id "
        "WHERE r.roast_id = ?",
        (roast_id,),
    ).fetchone()
    return row["author_pubkey"]


def test_ingest_file_sets_author_pubkey_from_source_ref_for_p2p_rows(conn) -> None:
    result = ingest_file(
        conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="abc123pub:00000007"
    )
    assert _author_pubkey_for(conn, result.record.roast_id) == "abc123pub"


def test_ingest_file_sets_author_pubkey_from_injected_local_identity(conn) -> None:
    result = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", local_pubkey_hex="mylocalpubkey")
    assert _author_pubkey_for(conn, result.record.roast_id) == "mylocalpubkey"


def test_ingest_file_leaves_author_pubkey_null_for_local_row_with_no_local_identity(
    conn, monkeypatch, tmp_path: Path
) -> None:
    """Getting the local identity must not create one as a side effect of a
    plain ingest -- with no identity.json on disk and nothing injected, the
    column is left NULL, and no identity file is created along the way."""
    home = tmp_path / "isolated_home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    result = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog")

    assert _author_pubkey_for(conn, result.record.roast_id) is None
    assert not (home / ".config" / "roastmesh" / "identity.json").exists()


def test_ingest_file_local_pubkey_hex_none_is_respected_not_overridden(conn) -> None:
    """local_pubkey_hex=None must mean "no local identity", distinct from
    the caller simply not passing the argument at all (which autodetects)
    -- confirms the _UNSET sentinel actually works as a sentinel."""
    result = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", local_pubkey_hex=None)
    assert _author_pubkey_for(conn, result.record.roast_id) is None


def test_refresh_known_sources_backfills_author_pubkey_for_a_p2p_row(conn) -> None:
    result = ingest_file(
        conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="peerpubkeyhex:00000001"
    )
    # Simulate "ingested before author_pubkey existed": null it out directly.
    conn.execute("UPDATE sources SET author_pubkey = NULL")
    conn.commit()
    assert _author_pubkey_for(conn, result.record.roast_id) is None

    results = refresh_known_sources(conn)

    assert len(results) == 1
    assert results[0].error is None
    assert _author_pubkey_for(conn, result.record.roast_id) == "peerpubkeyhex"


def test_refresh_known_sources_backfills_author_pubkey_for_a_local_row(conn) -> None:
    result = ingest_file(conn, FIXTURES_DIR / "hottop_1.alog", local_pubkey_hex="mylocalpubkey")
    conn.execute("UPDATE sources SET author_pubkey = NULL")
    conn.commit()
    assert _author_pubkey_for(conn, result.record.roast_id) is None

    # refresh_known_sources doesn't take a local_pubkey_hex parameter, so
    # this exercises ingest_file's own autodetection path -- point it at an
    # isolated HOME with a real identity so the result is deterministic
    # rather than depending on whatever happens to run this test suite.
    results = refresh_known_sources(conn)

    assert len(results) == 1
    assert results[0].error is None
    # No local identity was set up for this test, so autodetection finds
    # nothing -- still a real backfill attempt, just to NULL, not a no-op.
    # (See the paired monkeypatched-identity variant below for the
    # "actually resolves to a real pubkey" case.)


def test_refresh_known_sources_backfills_author_pubkey_for_a_local_row_with_a_real_identity(
    conn, monkeypatch, tmp_path: Path
) -> None:
    from roastmesh.identity import generate_identity, save_identity

    home = tmp_path / "isolated_home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    identity = generate_identity()
    save_identity(identity)  # writes to the now-isolated default_identity_path()

    result = ingest_file(conn, FIXTURES_DIR / "hottop_1.alog")
    assert _author_pubkey_for(conn, result.record.roast_id) == identity.public_key_hex

    conn.execute("UPDATE sources SET author_pubkey = NULL")
    conn.commit()

    results = refresh_known_sources(conn)

    assert len(results) == 1
    assert results[0].error is None
    assert _author_pubkey_for(conn, result.record.roast_id) == identity.public_key_hex


def test_refresh_known_sources_backfill_does_not_break_own_roast_tagging(conn) -> None:
    """A regression guard in the same spirit as
    test_refresh_known_sources_preserves_own_roast_tagging above: adding
    the author_pubkey backfill to the same code path must not disturb
    is_user_log."""
    ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", is_user_log=True, local_pubkey_hex="me")
    conn.execute("UPDATE sources SET author_pubkey = NULL")
    conn.commit()

    refresh_known_sources(conn)

    own = repo.search_roasts(conn, own_only=True)
    assert len(own) == 1
    assert own[0].is_user_log is True


# ---------------------------------------------------------------------------
# search_roasts: user_pubkey, favorites_only, and the machine_key fallback
# to the roast's owner's declared machine.
# ---------------------------------------------------------------------------

def test_search_machine_key_still_matches_a_roasts_own_machine_exactly(conn) -> None:
    """The existing, unwidened case: a roast whose own machine_key is a
    real (non-"unknown") value must still be found by an exact --machine
    match, same as before this phase's fallback was added."""
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog")
    ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog")

    results = repo.search_roasts(conn, machine_key="hottop")

    assert len(results) == 1
    assert results[0].machine_key == "hottop"


def test_search_machine_key_falls_back_to_owners_declared_machine_when_roast_has_none(conn) -> None:
    """The plan's chosen behavior: a roast whose .alog recorded no machine
    (machine_key == "unknown", e.g. a blank roastertype field) is still
    found by --machine when it matches the *owner's* declared machine from
    their synced profile -- this only ever widens results."""
    result = ingest_file(
        conn, FIXTURES_DIR / "alexzhu_1.alog", source_type="p2p", source_ref="ownerpubkey:00000001"
    )
    assert result.record.machine_key == "unknown"  # alexzhu_1.alog has a blank roastertype
    repo.upsert_user_from_profile(
        conn,
        pubkey_hex="ownerpubkey",
        display_name="Amber Chaff",
        machine_key="aillio_bullet",
        machine_display="Aillio Bullet R1",
        profile_updated_at="2026-01-01T00:00:00Z",
    )

    results = repo.search_roasts(conn, machine_key="aillio_bullet")

    assert len(results) == 1
    assert results[0].roast_id == result.record.roast_id


def test_search_machine_key_fallback_does_not_leak_other_machines_in(conn) -> None:
    """The fallback must not turn --machine into "everything with an
    unknown machine_key" -- only the specific owner-declared machine being
    searched for should match."""
    unknown_result = ingest_file(
        conn, FIXTURES_DIR / "alexzhu_1.alog", source_type="p2p", source_ref="ownerpubkey:00000001"
    )
    repo.upsert_user_from_profile(
        conn,
        pubkey_hex="ownerpubkey",
        display_name="Amber Chaff",
        machine_key="aillio_bullet",
        machine_display="Aillio Bullet R1",
        profile_updated_at="2026-01-01T00:00:00Z",
    )
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog")  # a real, different machine_key

    results = repo.search_roasts(conn, machine_key="kaleido_serial")  # matches neither

    assert results == []
    # Sanity: the unknown-machine roast is still findable via the correct filter.
    assert len(repo.search_roasts(conn, machine_key="aillio_bullet")) == 1
    assert unknown_result.record.roast_id in {r.roast_id for r in repo.search_roasts(conn, machine_key="aillio_bullet")}


def test_search_user_pubkey_filters_to_that_authors_roasts(conn) -> None:
    r1 = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="alice:00000001")
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog", source_type="p2p", source_ref="bob:00000001")

    results = repo.search_roasts(conn, user_pubkey="alice")

    assert len(results) == 1
    assert results[0].roast_id == r1.record.roast_id


def test_search_user_pubkey_finds_roasts_even_without_a_users_row(conn) -> None:
    """s.author_pubkey, not u.pubkey_hex, is the source of truth: a --user
    lookup by pubkey must work even for an author whose profile has never
    synced (no `users` row exists for them yet)."""
    r1 = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="nevers:00000001")

    results = repo.search_roasts(conn, user_pubkey="nevers")

    assert len(results) == 1
    assert results[0].roast_id == r1.record.roast_id


def test_search_favorites_only_filters_to_favorited_authors(conn) -> None:
    r1 = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="alice:00000001")
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog", source_type="p2p", source_ref="bob:00000001")
    repo.upsert_user_from_profile(
        conn, pubkey_hex="alice", display_name="Alice", machine_key=None,
        machine_display=None, profile_updated_at="2026-01-01T00:00:00Z",
    )
    repo.upsert_user_from_profile(
        conn, pubkey_hex="bob", display_name="Bob", machine_key=None,
        machine_display=None, profile_updated_at="2026-01-01T00:00:00Z",
    )

    assert repo.search_roasts(conn, favorites_only=True) == []

    repo.set_user_favorite(conn, "alice", True)

    results = repo.search_roasts(conn, favorites_only=True)
    assert len(results) == 1
    assert results[0].roast_id == r1.record.roast_id


def test_search_favorites_only_excludes_roasts_from_an_unknown_author(conn) -> None:
    """No `users` row at all (author never synced a profile) means never
    favorited -- favorites_only must not accidentally include them."""
    ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="stranger:00000001")

    assert repo.search_roasts(conn, favorites_only=True) == []


def test_search_collapses_near_duplicate_entries_from_the_same_peer_by_default(conn) -> None:
    """A peer re-exporting/re-publishing the same physical roast several
    times in one sitting (incremental saves, say) produces several feed
    entries with genuinely different bytes -- content-hash dedup correctly
    doesn't collapse those, so without this they'd show up in search as
    unrelated separate roasts sharing a title. Confirmed against a real
    corpus: same-author/same-title/same-day groups with roast_epoch values
    24-90 minutes apart."""
    r1 = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="alice:00000001")
    r2 = ingest_file(conn, FIXTURES_DIR / "kaleido_2.alog", source_type="p2p", source_ref="alice:00000002")
    r3 = ingest_file(conn, FIXTURES_DIR / "kaleido_3.alog", source_type="p2p", source_ref="alice:00000003")

    # r1 and r2: same roast, re-exported 30 minutes later -- must collapse
    # to just the later one. r3: four hours after r1, same author/title/day
    # -- a genuinely separate session of the same bean, must NOT collapse.
    base_epoch = 1_700_000_000
    for roast_id, epoch in (
        (r1.record.roast_id, base_epoch),
        (r2.record.roast_id, base_epoch + 1800),
        (r3.record.roast_id, base_epoch + 4 * 3600),
    ):
        conn.execute(
            "UPDATE roasts SET title = ?, roast_date = ?, roast_epoch = ? WHERE roast_id = ?",
            ("Same Bean Roast", "2026-01-01", epoch, roast_id),
        )
    conn.commit()

    default_results = repo.search_roasts(conn)
    assert {row.roast_id for row in default_results} == {r2.record.roast_id, r3.record.roast_id}

    all_results = repo.search_roasts(conn, include_near_duplicates=True)
    assert {row.roast_id for row in all_results} == {
        r1.record.roast_id, r2.record.roast_id, r3.record.roast_id,
    }


def test_search_near_duplicate_collapsing_prefers_the_higher_author_seq_on_a_tied_epoch(conn) -> None:
    """A notes-only edit republished as a fresh, unlinked entry (the exact
    scenario near-duplicate collapsing exists for) carries the SAME
    roast_epoch as the original -- only the notes text changed, not the
    roast's own recorded start time. Confirmed as a real regression on a
    live corpus: plain max(roast_epoch) ties, and silently kept whichever
    row SQL happened to return first -- the stale one, discarding an edit
    the user had just made. author_seq (feed append order) must be the
    tiebreaker whenever both sides of a tie have one."""
    r1 = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="alice:00000010")
    r2 = ingest_file(conn, FIXTURES_DIR / "kaleido_2.alog", source_type="p2p", source_ref="alice:00000011")
    conn.execute(
        "UPDATE sources SET author_seq = 10 WHERE source_id = ?",
        (conn.execute("SELECT source_id FROM roasts WHERE roast_id = ?", (r1.record.roast_id,)).fetchone()[0],),
    )
    conn.execute(
        "UPDATE sources SET author_seq = 11 WHERE source_id = ?",
        (conn.execute("SELECT source_id FROM roasts WHERE roast_id = ?", (r2.record.roast_id,)).fetchone()[0],),
    )
    tied_epoch = 1_787_796_436
    for roast_id in (r1.record.roast_id, r2.record.roast_id):
        conn.execute(
            "UPDATE roasts SET title = ?, roast_date = ?, roast_epoch = ? WHERE roast_id = ?",
            ("Kaafa Anderacha Dark", "2026-08-26", tied_epoch, roast_id),
        )
    conn.commit()

    results = repo.search_roasts(conn)

    assert len(results) == 1
    assert results[0].roast_id == r2.record.roast_id  # the higher author_seq -- the newer publish


def test_search_near_duplicate_collapsing_ignores_rows_missing_an_epoch(conn) -> None:
    """A row with no roast_epoch can't be time-compared at all -- it must
    never be silently dropped just for sharing a title/date with another
    entry that does have one."""
    r1 = ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="alice:00000001")
    r2 = ingest_file(conn, FIXTURES_DIR / "kaleido_2.alog", source_type="p2p", source_ref="alice:00000002")
    conn.execute(
        "UPDATE roasts SET title = ?, roast_date = ?, roast_epoch = ? WHERE roast_id = ?",
        ("Same Bean Roast", "2026-01-01", 1_700_000_000, r1.record.roast_id),
    )
    conn.execute(
        "UPDATE roasts SET title = ?, roast_date = ?, roast_epoch = NULL WHERE roast_id = ?",
        ("Same Bean Roast", "2026-01-01", r2.record.roast_id),
    )
    conn.commit()

    results = repo.search_roasts(conn)
    assert {row.roast_id for row in results} == {r1.record.roast_id, r2.record.roast_id}


# ---------------------------------------------------------------------------
# Users repository: upsert, favorite, likes, listing, distinct machine keys.
# ---------------------------------------------------------------------------

def test_upsert_user_from_profile_inserts_then_updates(conn) -> None:
    repo.upsert_user_from_profile(
        conn, pubkey_hex="alice", display_name="Alice", machine_key="aillio_bullet",
        machine_display="Aillio Bullet R1", profile_updated_at="2026-01-01T00:00:00Z",
        seen_at="2026-01-01T00:00:00Z",
    )
    first = repo.find_user(conn, "alice")
    assert first["display_name"] == "Alice"
    assert first["first_seen"] == "2026-01-01T00:00:00Z"
    assert first["last_seen"] == "2026-01-01T00:00:00Z"

    repo.upsert_user_from_profile(
        conn, pubkey_hex="alice", display_name="Alice Renamed", machine_key="kaleido_m2",
        machine_display="Kaleido M2", profile_updated_at="2026-02-01T00:00:00Z",
        seen_at="2026-02-01T00:00:00Z",
    )
    second = repo.find_user(conn, "alice")
    assert second["display_name"] == "Alice Renamed"
    assert second["machine_key"] == "kaleido_m2"
    assert second["first_seen"] == "2026-01-01T00:00:00Z"  # unchanged
    assert second["last_seen"] == "2026-02-01T00:00:00Z"  # advanced


def test_upsert_user_from_profile_never_sets_is_favorite(conn) -> None:
    repo.upsert_user_from_profile(
        conn, pubkey_hex="alice", display_name="Alice", machine_key=None,
        machine_display=None, profile_updated_at="2026-01-01T00:00:00Z",
    )
    repo.set_user_favorite(conn, "alice", True)

    # A later profile refresh (e.g. a re-sync) must not clear a local
    # favorite -- is_favorite never leaves this machine and a peer's
    # profile has no opinion on it.
    repo.upsert_user_from_profile(
        conn, pubkey_hex="alice", display_name="Alice Renamed", machine_key=None,
        machine_display=None, profile_updated_at="2026-02-01T00:00:00Z",
    )

    assert bool(repo.find_user(conn, "alice")["is_favorite"]) is True


def test_find_user_returns_none_for_unknown_pubkey(conn) -> None:
    assert repo.find_user(conn, "nobody") is None


def test_set_user_favorite_returns_false_for_an_unknown_user(conn) -> None:
    assert repo.set_user_favorite(conn, "nobody", True) is False


def test_set_user_favorite_toggles_on_a_known_user(conn) -> None:
    repo.upsert_user_from_profile(
        conn, pubkey_hex="alice", display_name="Alice", machine_key=None,
        machine_display=None, profile_updated_at="2026-01-01T00:00:00Z",
    )
    assert repo.set_user_favorite(conn, "alice", True) is True
    assert bool(repo.find_user(conn, "alice")["is_favorite"]) is True
    assert repo.set_user_favorite(conn, "alice", False) is True
    assert bool(repo.find_user(conn, "alice")["is_favorite"]) is False


def test_add_and_remove_user_like(conn) -> None:
    # "bob" has to be a known user (here, via a synced profile) for
    # list_users' candidate set to include him at all -- see
    # test_list_users_with_roasts_only_excludes_users_with_no_ingested_roast
    # for the two ways a pubkey becomes "known".
    repo.upsert_user_from_profile(
        conn, pubkey_hex="bob", display_name="Bob", machine_key=None,
        machine_display=None, profile_updated_at="2026-01-01T00:00:00Z",
    )
    repo.add_user_like(conn, "alice", "bob")
    repo.add_user_like(conn, "carol", "bob")

    users = {u.pubkey_hex: u for u in repo.list_users(conn, with_roasts_only=False)}
    assert users["bob"].like_count == 2

    assert repo.remove_user_like(conn, "alice", "bob") is True
    users = {u.pubkey_hex: u for u in repo.list_users(conn, with_roasts_only=False)}
    assert users["bob"].like_count == 1

    assert repo.remove_user_like(conn, "alice", "bob") is False  # already gone


def test_add_user_like_is_idempotent_per_liker_subject_pair(conn) -> None:
    repo.add_user_like(conn, "alice", "bob", liked_at="2026-01-01T00:00:00Z")
    repo.add_user_like(conn, "alice", "bob", liked_at="2026-02-01T00:00:00Z")  # re-like, e.g. a re-sync

    count = conn.execute(
        "SELECT COUNT(*) c FROM user_likes WHERE liker_pubkey = 'alice' AND subject_pubkey = 'bob'"
    ).fetchone()["c"]
    assert count == 1


def test_list_users_with_roasts_only_excludes_users_with_no_ingested_roast(conn) -> None:
    ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="alice:00000001")
    repo.upsert_user_from_profile(  # bob is known but has never published anything ingested
        conn, pubkey_hex="bob", display_name="Bob", machine_key=None,
        machine_display=None, profile_updated_at="2026-01-01T00:00:00Z",
    )

    with_roasts = {u.pubkey_hex for u in repo.list_users(conn, with_roasts_only=True)}
    assert with_roasts == {"alice"}

    everyone = {u.pubkey_hex for u in repo.list_users(conn, with_roasts_only=False)}
    assert everyone == {"alice", "bob"}


def test_list_users_reports_roast_and_like_counts(conn) -> None:
    ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="alice:00000001")
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog", source_type="p2p", source_ref="alice:00000002")
    repo.add_user_like(conn, "bob", "alice")

    users = {u.pubkey_hex: u for u in repo.list_users(conn)}
    assert users["alice"].roast_count == 2
    assert users["alice"].like_count == 1


def test_list_users_filters_by_machine_and_favorites(conn) -> None:
    ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog", source_type="p2p", source_ref="alice:00000001")
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog", source_type="p2p", source_ref="bob:00000001")
    repo.upsert_user_from_profile(
        conn, pubkey_hex="alice", display_name="Alice", machine_key="aillio_bullet",
        machine_display="Aillio Bullet R1", profile_updated_at="2026-01-01T00:00:00Z",
    )
    repo.upsert_user_from_profile(
        conn, pubkey_hex="bob", display_name="Bob", machine_key="kaleido_m2",
        machine_display="Kaleido M2", profile_updated_at="2026-01-01T00:00:00Z",
    )
    repo.set_user_favorite(conn, "alice", True)

    assert {u.pubkey_hex for u in repo.list_users(conn, machine_key="aillio_bullet")} == {"alice"}
    assert {u.pubkey_hex for u in repo.list_users(conn, favorites_only=True)} == {"alice"}


def test_find_distinct_machine_keys(conn) -> None:
    ingest_file(conn, FIXTURES_DIR / "kaleido_1.alog")
    ingest_file(conn, FIXTURES_DIR / "hottop_1.alog")
    ingest_file(conn, FIXTURES_DIR / "hottop_2.alog")  # same machine_key as above, no duplicate expected

    keys = repo.find_distinct_machine_keys(conn)

    assert keys == sorted(set(keys))  # sorted, and DISTINCT actually deduplicated
    assert "hottop" in keys
    assert "kaleido_serial" in keys
    assert keys.count("hottop") == 1


def test_concurrent_migrate_does_not_race_on_added_columns(tmp_path: Path) -> None:
    """serve() opens two connections at startup -- the version-gated refresh and
    the replication loop -- and both call migrate(). On the first launch after
    an upgrade that adds a column they raced to ALTER TABLE ADD COLUMN: one won,
    the other had read the pre-ALTER schema and failed with "duplicate column
    name". Observed live on the Pi's first 0.6.15 launch as "replication: pass
    failed: OperationalError('duplicate column name: blob_local')".

    Reproduces the real scenario: an *existing* WAL database (an upgrade, not a
    fresh file) that is missing only the newest column, then concurrent
    connect()s racing on the one ALTER. _apply_added_columns must treat a
    concurrently-added column as done, not as an error.
    """
    import threading
    from roastmesh.index import db as dbmod

    db = tmp_path / "index.sqlite3"

    # Build a pre-upgrade DB: fully migrated and WAL-mode, but without the
    # newest added column -- exactly what a node carries into its first launch
    # on a version that introduces one.
    original = dbmod._ADDED_COLUMNS
    dbmod._ADDED_COLUMNS = [c for c in original if c != ("sources", "blob_local", "INTEGER NOT NULL DEFAULT 1")]
    try:
        connect(db).close()
    finally:
        dbmod._ADDED_COLUMNS = original
    import sqlite3
    pre = sqlite3.connect(db)
    assert "blob_local" not in {r[1] for r in pre.execute("PRAGMA table_info(sources)")}
    pre.close()

    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        try:
            barrier.wait()          # release all threads into the ALTER race at once
            connect(db).close()
        except Exception as exc:    # noqa: BLE001 -- the whole point is to catch it
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    conn = connect(db)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
    conn.close()
    assert "blob_local" in cols     # the migration still actually happened


def _copy_fixture(tmp_path: Path, name: str = "kaleido_1.alog") -> Path:
    # Tests that edit a roast's file must never write to the checked-in
    # fixtures under tests/fixtures/ -- copy one into tmp_path first.
    dest = tmp_path / name
    dest.write_bytes((FIXTURES_DIR / name).read_bytes())
    return dest


def test_edit_unpublished_roast_notes_updates_the_same_row_in_place(conn, tmp_path: Path) -> None:
    path = _copy_fixture(tmp_path)
    first = ingest_file(conn, path, is_user_log=True)
    roast_id = first.record.roast_id

    result = edit_unpublished_roast_notes(conn, roast_id, roasting_notes="edited before ever publishing")
    assert result.error is None
    assert result.record.roast_id == roast_id  # same row, not a new one

    row_count = conn.execute("SELECT COUNT(*) FROM roasts").fetchone()[0]
    assert row_count == 1
    stored = conn.execute("SELECT roasting_notes FROM roasts WHERE roast_id = ?", (roast_id,)).fetchone()
    assert stored["roasting_notes"] == "edited before ever publishing"

    # the file on disk was actually rewritten, not just the index row
    from roastmesh.alog.edit import parse_for_edit
    on_disk, _fmt = parse_for_edit(path.read_bytes())
    assert on_disk["roastingnotes"] == "edited before ever publishing"


def test_edit_unpublished_roast_notes_updates_fts(conn, tmp_path: Path) -> None:
    path = _copy_fixture(tmp_path)
    first = ingest_file(conn, path, is_user_log=True)
    roast_id = first.record.roast_id

    edit_unpublished_roast_notes(conn, roast_id, roasting_notes="a very distinctive searchable phrase")

    rows = repo.search_roasts(conn, text="distinctive searchable phrase")
    assert any(r.roast_id == roast_id for r in rows)


def test_edit_unpublished_roast_notes_refuses_an_already_published_roast(conn, tmp_path: Path) -> None:
    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    path = _copy_fixture(tmp_path)
    append_entry(feed_dir, identity, path, timestamp="2026-01-01T00:00:00Z")
    results = ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex)
    roast_id = results[0].record.roast_id

    result = edit_unpublished_roast_notes(conn, roast_id, roasting_notes="should be refused")
    assert result.error is not None
    assert "already published" in result.error

    # nothing was actually changed
    stored = conn.execute("SELECT roasting_notes FROM roasts WHERE roast_id = ?", (roast_id,)).fetchone()
    assert stored["roasting_notes"] != "should be refused"


def test_edit_unpublished_roast_notes_rejects_an_unknown_roast_id(conn) -> None:
    result = edit_unpublished_roast_notes(conn, "not-a-real-roast-id", roasting_notes="x")
    assert result.error is not None
    assert "no such roast" in result.error


# ---------------------------------------------------------------------------
# apply_delivered_edit: turning a delivered cross-device edit
# (device_sync.EDITED_DIR_NAME/<owner_pubkey>/<author_seq>.alog) into this
# identity's own actual, visible update -- confirmed as a real gap
# otherwise: a delivered edit just sat there forever as an inert file,
# invisible to search, which looked exactly like data loss to a user
# checking search alone.
#
# Looked up by (author_pubkey, author_seq), NOT roast_id: confirmed as a
# real, live bug during the very first end-to-end test of this function --
# roast_id is a fresh random UUID minted independently by every machine
# that ingests the same content, so a roast_id the EDITING device knows is
# meaningless on the OWNING device, which is the one this function runs on.
# author_seq is this identity's own feed sequence number, the one thing
# both sides actually agree on. There is no "not yet published" case to
# handle here (unlike edit_unpublished_roast_notes): the only way another
# device could see this roast at all, for it to deliver an edit back, is
# via that same public feed entry -- so it is always already published.
# ---------------------------------------------------------------------------

def test_apply_delivered_edit_publishes_a_superseding_entry_for_an_already_published_roast(
    conn, tmp_path: Path,
) -> None:
    from roastmesh.alog.edit import set_notes

    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    path = _copy_fixture(tmp_path)
    entry = append_entry(feed_dir, identity, path, timestamp="2026-01-01T00:00:00Z")
    results = ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex)
    original_roast_id = results[0].record.roast_id

    new_bytes = set_notes(path.read_bytes(), roasting_notes="delivered edit, already published")
    result = apply_delivered_edit(conn, feed_dir, identity, entry.seq, new_bytes)

    assert result.error is None
    assert result.record.roast_id != original_roast_id  # a NEW row, the original is untouched

    original = conn.execute(
        "SELECT roasting_notes FROM roasts WHERE roast_id = ?", (original_roast_id,)
    ).fetchone()
    assert original["roasting_notes"] is None  # unedited -- append-only, never touched

    default_search = repo.search_roasts(conn)
    assert len(default_search) == 1
    assert default_search[0].roast_id == result.record.roast_id

    with_superseded = repo.search_roasts(conn, include_superseded=True)
    assert len(with_superseded) == 2


def test_apply_delivered_edit_refuses_a_seq_that_is_not_this_identitys_own(conn, tmp_path: Path) -> None:
    owner_identity = generate_identity()
    someone_else = generate_identity()
    feed_dir = tmp_path / "feed"
    path = _copy_fixture(tmp_path)
    entry = append_entry(feed_dir, owner_identity, path, timestamp="2026-01-01T00:00:00Z")
    results = ingest_feed(conn, feed_dir, expected_pubkey_hex=owner_identity.public_key_hex)
    roast_id = results[0].record.roast_id

    # someone_else has no entry at this seq in THEIR OWN feed -- must not
    # find (and overwrite) owner_identity's entry just because the seq
    # number happens to match.
    result = apply_delivered_edit(conn, tmp_path / "someone_elses_feed", someone_else, entry.seq, b"forged content")
    assert result.error is not None
    assert "no entry at seq" in result.error

    stored = conn.execute("SELECT roasting_notes FROM roasts WHERE roast_id = ?", (roast_id,)).fetchone()
    assert stored["roasting_notes"] is None  # untouched


def test_apply_delivered_edit_rejects_an_unknown_author_seq(conn, tmp_path: Path) -> None:
    identity = generate_identity()
    result = apply_delivered_edit(conn, tmp_path / "feed", identity, 999, b"content")
    assert result.error is not None
    assert "no entry at seq 999" in result.error


# ---------------------------------------------------------------------------
# resolve_to_latest_roast_id: following a supersede chain forward so a
# caller holding a stale roast_id (e.g. a GUI search-results table that
# hasn't refreshed yet) always gets the current version, not the frozen
# original -- confirmed as a real, reported bug: a user who reopened the
# very roast they just edited, fast enough to beat the GUI's own
# (asynchronous) refresh, saw the stale pre-edit content.
# ---------------------------------------------------------------------------

def test_resolve_to_latest_roast_id_follows_a_single_supersede(conn, tmp_path: Path) -> None:
    from roastmesh.alog.edit import set_notes

    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    path = _copy_fixture(tmp_path)
    append_entry(feed_dir, identity, path, timestamp="2026-01-01T00:00:00Z")
    results = ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex)
    original_roast_id = results[0].record.roast_id

    new_bytes = set_notes(path.read_bytes(), roasting_notes="the edit")
    new_entry = apply_delivered_edit(conn, feed_dir, identity, 0, new_bytes)

    assert repo.resolve_to_latest_roast_id(conn, original_roast_id) == new_entry.record.roast_id


def test_resolve_to_latest_roast_id_follows_a_multi_hop_chain(conn, tmp_path: Path) -> None:
    from roastmesh.alog.edit import set_notes

    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    path = _copy_fixture(tmp_path)
    append_entry(feed_dir, identity, path, timestamp="2026-01-01T00:00:00Z")
    results = ingest_feed(conn, feed_dir, expected_pubkey_hex=identity.public_key_hex)
    original_roast_id = results[0].record.roast_id

    first_edit = apply_delivered_edit(
        conn, feed_dir, identity, 0, set_notes(path.read_bytes(), roasting_notes="first edit"),
    )
    second_edit = apply_delivered_edit(
        conn, feed_dir, identity, 1, set_notes(path.read_bytes(), roasting_notes="second edit"),
    )

    # From the ORIGINAL id, and from the first edit's own (now itself
    # superseded) id -- both must resolve all the way to the tip.
    assert repo.resolve_to_latest_roast_id(conn, original_roast_id) == second_edit.record.roast_id
    assert repo.resolve_to_latest_roast_id(conn, first_edit.record.roast_id) == second_edit.record.roast_id
    assert repo.resolve_to_latest_roast_id(conn, second_edit.record.roast_id) == second_edit.record.roast_id


def test_resolve_to_latest_roast_id_returns_unchanged_for_a_never_superseded_roast(conn, tmp_path: Path) -> None:
    path = _copy_fixture(tmp_path)
    result = ingest_file(conn, path, is_user_log=True)
    assert repo.resolve_to_latest_roast_id(conn, result.record.roast_id) == result.record.roast_id
