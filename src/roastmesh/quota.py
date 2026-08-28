"""Quota enforcement at the metadata-check boundary (ARCHITECTURE.md's Abuse
Resistance section): before fetching a peer's actual profile bytes, decide
-- from cheap metadata alone -- how much of what they're offering is worth
fetching at all. Evaluated locally and independently by each client; a peer
that violates these is simply not fully synced by whoever noticed, no
consensus or blocklist involved.

Pure logic, no I/O -- callers (net.sync_with_peer) supply the entries.
"""
from __future__ import annotations

from dataclasses import dataclass

from roastmesh.feed import FeedEntry


@dataclass
class QuotaLimits:
    # Defaults are generous-but-bounded guesses, not load-bearing precise
    # numbers -- tunable per the doc's own "ship without further sybil
    # defense; add... only if it becomes a real problem" stance. Sized
    # against real fixture files (tens of KB) and a plausible hobbyist
    # publish rate (a handful of roasts/day, not hundreds).
    max_files_per_feed: int = 5000
    max_bytes_per_feed: int = 200 * 1024 * 1024
    max_bytes_per_file: int = 1 * 1024 * 1024  # ARCHITECTURE.md's own number: ".alog has no business exceeding ~1 MB"
    max_files_per_day: int = 50  # per calendar day, by entries' own declared timestamps


@dataclass
class QuotaCheckResult:
    allowed_count: int  # how many of `candidates`, in order, are within budget
    total_count: int
    reason: str | None  # why it stopped, if it stopped before exhausting candidates

    @property
    def held_back(self) -> int:
        return self.total_count - self.allowed_count


def _day_key(timestamp: str) -> str:
    # entries' timestamps are ISO 8601; the calendar-date prefix is a stable
    # sort/group key without needing full datetime parsing.
    return timestamp[:10]


def check_feed_metadata(
    existing: list[FeedEntry],
    candidates: list[FeedEntry],
    limits: QuotaLimits,
) -> QuotaCheckResult:
    """How many of `candidates` (in order) can be admitted on top of
    `existing`, without any prefix breaching a limit. Entries form a strict
    append-only chain, so "stop at the first violation" is the only sound
    behavior -- there's no such thing as admitting entry K+1 while excluding
    entry K.
    """
    day_counts: dict[str, int] = {}
    for e in existing:
        day_counts[_day_key(e.timestamp)] = day_counts.get(_day_key(e.timestamp), 0) + 1

    count = len(existing)
    total_bytes = sum(e.size_bytes for e in existing)

    for i, entry in enumerate(candidates):
        if entry.size_bytes > limits.max_bytes_per_file:
            return QuotaCheckResult(i, len(candidates),
                                     f"entry {entry.seq}: {entry.size_bytes} bytes exceeds max_bytes_per_file "
                                     f"({limits.max_bytes_per_file})")
        if count + 1 > limits.max_files_per_feed:
            return QuotaCheckResult(i, len(candidates),
                                     f"entry {entry.seq}: would exceed max_files_per_feed ({limits.max_files_per_feed})")
        if total_bytes + entry.size_bytes > limits.max_bytes_per_feed:
            return QuotaCheckResult(i, len(candidates),
                                     f"entry {entry.seq}: would exceed max_bytes_per_feed ({limits.max_bytes_per_feed})")
        day = _day_key(entry.timestamp)
        if day_counts.get(day, 0) + 1 > limits.max_files_per_day:
            return QuotaCheckResult(i, len(candidates),
                                     f"entry {entry.seq}: would exceed max_files_per_day ({limits.max_files_per_day}) "
                                     f"for {day}")

        count += 1
        total_bytes += entry.size_bytes
        day_counts[day] = day_counts.get(day, 0) + 1

    return QuotaCheckResult(len(candidates), len(candidates), None)
