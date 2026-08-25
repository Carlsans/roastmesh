from roastnet.feed import FeedEntry
from roastnet.quota import QuotaLimits, check_feed_metadata


def _entry(seq: int, *, size_bytes: int = 1000, day: str = "2026-01-01") -> FeedEntry:
    return FeedEntry(
        seq=seq, content_sha256=f"hash{seq}", timestamp=f"{day}T00:00:0{seq % 10}Z",
        prev_hash="prev", size_bytes=size_bytes, signature="sig",
    )


def test_all_candidates_admitted_when_within_every_limit() -> None:
    candidates = [_entry(i, day=f"2026-01-{i + 1:02d}") for i in range(3)]
    result = check_feed_metadata([], candidates, QuotaLimits())
    assert result.allowed_count == 3
    assert result.reason is None
    assert result.held_back == 0


def test_max_files_per_feed_stops_at_the_cap() -> None:
    limits = QuotaLimits(max_files_per_feed=2)
    candidates = [_entry(i, day=f"2026-01-{i + 1:02d}") for i in range(5)]
    result = check_feed_metadata([], candidates, limits)
    assert result.allowed_count == 2
    assert "max_files_per_feed" in result.reason


def test_max_files_per_feed_accounts_for_existing_entries() -> None:
    limits = QuotaLimits(max_files_per_feed=3)
    existing = [_entry(i, day=f"2026-01-{i + 1:02d}") for i in range(2)]
    candidates = [_entry(i, day=f"2026-02-{i + 1:02d}") for i in range(2, 5)]
    result = check_feed_metadata(existing, candidates, limits)
    assert result.allowed_count == 1  # 2 existing + 1 more = cap of 3


def test_max_bytes_per_feed_stops_at_the_right_entry() -> None:
    limits = QuotaLimits(max_bytes_per_feed=2500)
    candidates = [_entry(i, size_bytes=1000, day=f"2026-01-{i + 1:02d}") for i in range(5)]
    result = check_feed_metadata([], candidates, limits)
    assert result.allowed_count == 2  # 1000 + 1000 = 2000 <= 2500; + 1000 = 3000 > 2500
    assert "max_bytes_per_feed" in result.reason


def test_single_oversized_entry_excludes_it_and_everything_after() -> None:
    limits = QuotaLimits(max_bytes_per_file=500)
    candidates = [
        _entry(0, size_bytes=100, day="2026-01-01"),
        _entry(1, size_bytes=100, day="2026-01-02"),
        _entry(2, size_bytes=999, day="2026-01-03"),  # too big
        _entry(3, size_bytes=100, day="2026-01-04"),  # fine on its own, but can't skip entry 2
    ]
    result = check_feed_metadata([], candidates, limits)
    assert result.allowed_count == 2
    assert "max_bytes_per_file" in result.reason


def test_max_files_per_day_stops_once_a_day_hits_the_cap() -> None:
    limits = QuotaLimits(max_files_per_day=2)
    candidates = [_entry(i, day="2026-03-15") for i in range(4)]  # all same day
    result = check_feed_metadata([], candidates, limits)
    assert result.allowed_count == 2
    assert "max_files_per_day" in result.reason


def test_max_files_per_day_accounts_for_existing_entries_on_that_day() -> None:
    limits = QuotaLimits(max_files_per_day=2)
    existing = [_entry(0, day="2026-03-15")]
    candidates = [_entry(1, day="2026-03-15"), _entry(2, day="2026-03-15")]
    result = check_feed_metadata(existing, candidates, limits)
    assert result.allowed_count == 1  # 1 existing + 1 more = cap of 2


def test_different_days_dont_share_a_budget() -> None:
    limits = QuotaLimits(max_files_per_day=2)
    candidates = [
        _entry(0, day="2026-03-15"), _entry(1, day="2026-03-15"),
        _entry(2, day="2026-03-16"), _entry(3, day="2026-03-16"),
    ]
    result = check_feed_metadata([], candidates, limits)
    assert result.allowed_count == 4
    assert result.reason is None
