from __future__ import annotations

import shutil
from pathlib import Path

from roastmesh.feed import read_entries
from roastmesh.identity import generate_identity
from roastmesh.index import repository as repo
from roastmesh.index.db import connect
from roastmesh.watch_folder import publish_new_files

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURES_DIR.glob("*.alog"))[:2]


def test_publish_new_files_publishes_everything_dropped_in(tmp_path: Path) -> None:
    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    for path in FIXTURES:
        shutil.copy(path, watch_dir / path.name)

    published = publish_new_files(feed_dir, identity, watch_dir)

    assert len(published) == len(FIXTURES)
    assert len(read_entries(feed_dir)) == len(FIXTURES)


def test_publish_new_files_is_idempotent_on_a_second_scan(tmp_path: Path) -> None:
    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    shutil.copy(FIXTURES[0], watch_dir / FIXTURES[0].name)

    first = publish_new_files(feed_dir, identity, watch_dir)
    second = publish_new_files(feed_dir, identity, watch_dir)

    assert len(first) == 1
    assert second == []
    assert len(read_entries(feed_dir)) == 1


def test_publish_new_files_only_publishes_the_new_one_after_a_file_is_added(tmp_path: Path) -> None:
    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    shutil.copy(FIXTURES[0], watch_dir / FIXTURES[0].name)
    publish_new_files(feed_dir, identity, watch_dir)

    shutil.copy(FIXTURES[1], watch_dir / FIXTURES[1].name)
    second = publish_new_files(feed_dir, identity, watch_dir)

    assert len(second) == 1
    assert len(read_entries(feed_dir)) == 2


def test_publish_new_files_on_a_nonexistent_folder_is_a_noop(tmp_path: Path) -> None:
    identity = generate_identity()
    published = publish_new_files(tmp_path / "feed", identity, tmp_path / "does-not-exist")
    assert published == []


def test_publish_new_files_indexes_a_file_already_in_the_feed_but_not_yet_indexed(tmp_path: Path) -> None:
    """A file whose content is already in the feed (published in an earlier
    session, say, before the local index existed or was last rebuilt) must
    still end up with a sources/roasts row the first time this is called
    with a db_path -- not just files that are newly published this call."""
    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    shutil.copy(FIXTURES[0], watch_dir / FIXTURES[0].name)

    # No db_path here: the file lands in the feed but never gets indexed.
    publish_new_files(feed_dir, identity, watch_dir)

    db_path = tmp_path / "index.sqlite3"
    published = publish_new_files(feed_dir, identity, watch_dir, db_path=db_path)

    assert published == []  # already in the feed -- nothing new published
    conn = connect(db_path)
    try:
        assert len(repo.find_all_sources(conn)) == 1
    finally:
        conn.close()


def test_publish_new_files_does_not_duplicate_an_already_indexed_file(tmp_path: Path) -> None:
    identity = generate_identity()
    feed_dir = tmp_path / "feed"
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    shutil.copy(FIXTURES[0], watch_dir / FIXTURES[0].name)
    db_path = tmp_path / "index.sqlite3"

    publish_new_files(feed_dir, identity, watch_dir, db_path=db_path)
    publish_new_files(feed_dir, identity, watch_dir, db_path=db_path)

    conn = connect(db_path)
    try:
        assert len(repo.find_all_sources(conn)) == 1
    finally:
        conn.close()
