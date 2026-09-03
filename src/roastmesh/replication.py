"""Bounded, coverage-maximizing feed replication -- the retention policy.

Every node mirrors not just the feeds it syncs directly but feeds authored by
users it has never contacted, so a roast survives as long as *any* holder is
online (ARCHITECTURE.md's "every peer mirrors the entire corpus", finally
built). Disk is finite, so once the corpus outgrows the budget a node cannot
keep everything -- and the choice of *what* to keep is what decides how many
distinct roasts survive network-wide.

The rule, decided deliberately: **keep the rarest feeds.** When over budget,
evict the feeds the most other reachable peers already hold, and break ties by
keeping the feeds whose pubkey is XOR-closest to our own id (dht.distance) --
so the network's aggregate choice lands each feed on a stable ~K nodes and no
popular feed crowds a rare one off every disk. A feed nobody else holds is the
last thing anyone evicts.

Pure logic, no I/O (quota.py's shape): callers scan the disk and the ledger and
hand the data in. feed.py's held-feed scan and net.py's `_replication_loop`
supply it; this module only decides.

Self-authentication is what makes any of this safe to do at all: a feed's
entries are signed and hash-chained (feed.verify_feed), so re-serving a third
party's feed cannot forge or alter it -- a bad mirror can only withhold or
truncate, which degrades to "shorter feed", never to corrupt data. So holding
and re-serving a stranger's feed needs no new trust mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from roastmesh.dht import distance

# The mirror budget for replicated peer feeds (own feed lives in feed_dir and
# is never counted or evicted). 500 MB is generous against ARCHITECTURE.md's
# "10,000 profiles is ~100-300 MB" -- room for the whole foreseeable corpus,
# so a hobbyist network never actually evicts, while the policy is ready the
# day it would. `0` disables replication entirely.
DEFAULT_REPLICATION_BUDGET = 500 * 1024 * 1024

# How many feeds one acquisition round will try to pull. Bounds the work (and
# the connections) a single `_replication_loop` pass drives, the same way
# dht.MAX_SEARCH_ROUNDS bounds a lookup -- a node fills spare budget over many
# rounds, not in one burst.
MAX_ACQUIRE_PER_ROUND = 16

# Caps on attacker-influenced, gossip-grown state -- the cap_peers lesson
# (peers.py): a single hostile peer can name unboundedly many feeds and
# holders. `known_feeds` beyond this many are the least worth keeping (see
# cap_known_feeds); holders beyond this many per feed add nothing to a replica
# estimate that only needs to answer "rare or not".
MAX_KNOWN_FEEDS = 5000
MAX_HOLDERS_PER_FEED = 64


@dataclass(frozen=True)
class FeedHolding:
    """A feed we hold on disk right now, measured (never declared)."""
    pubkey: str
    entry_count: int
    total_bytes: int
    latest_seq: int


@dataclass(frozen=True)
class KnownFeed:
    """A feed we know exists, held or not. `total_bytes`/`latest_seq` are a
    peer's *declared* digest -- a pre-fetch hint only, never trusted for real
    budget accounting (a hostile holder can lie); acquisition still re-verifies
    and re-measures every byte that actually arrives."""
    pubkey: str
    latest_seq: int
    total_bytes: int
    entry_count: int


@dataclass
class RetentionPlan:
    keep: list[str] = field(default_factory=list)      # held feeds to retain
    evict: list[str] = field(default_factory=list)      # held feeds to drop to stubs
    acquire: list[str] = field(default_factory=list)    # feeds to pull this round


def _feed_distance(pubkey: str, my_id: bytes) -> int:
    """XOR distance between a feed's pubkey and our own id, both 32-byte
    Ed25519 keys. A malformed (non-hex) pubkey sorts to the far end rather
    than raising -- it should never have reached here (feed._PUBKEY_RE guards
    every write path), but a retention pass must not crash on one bad row."""
    try:
        return distance(bytes.fromhex(pubkey), my_id)
    except ValueError:
        return 1 << 256


def _keep_rank(pubkey: str, holder_counts: dict[str, int], my_id: bytes) -> tuple[int, int]:
    """Best-to-keep ordering key: rarest first (fewest other holders), then
    XOR-closest. Sorting held feeds by this and keeping the prefix that fits
    the budget is the whole policy -- so the rarest feed is the last evicted,
    and among equally-rare feeds each node keeps a different, id-determined
    slice, spreading coverage instead of everyone hoarding the same ones."""
    return (holder_counts.get(pubkey, 0), _feed_distance(pubkey, my_id))


def plan_retention(
    local: list[FeedHolding],
    known: dict[str, KnownFeed],
    holder_counts: dict[str, int],
    pinned: set[str],
    my_id: bytes,
    budget_bytes: int,
) -> RetentionPlan:
    """Decide which held feeds to keep, which to evict to stubs, and which
    absent/behind feeds to acquire this round -- all to maximize the number of
    distinct roasts that survive network-wide within `budget_bytes`.

    `pinned` (manually-added peers, favorited authors) are always kept and
    still count against the budget, so a user's deliberate choices are never
    evicted out from under them. Everything else held is the evictable pool.
    Feeds are atomic: a feed is a hash chain, so a full verified prefix is the
    only useful unit -- keep a whole feed or none of it.
    """
    held = {h.pubkey: h for h in local}
    bytes_of = {pk: h.total_bytes for pk, h in held.items()}

    pinned_held = [pk for pk in held if pk in pinned]
    evictable = [pk for pk in held if pk not in pinned]

    reserved = sum(bytes_of[pk] for pk in pinned_held)
    remaining = max(budget_bytes - reserved, 0)

    plan = RetentionPlan()
    plan.keep.extend(pinned_held)

    # Keep evictable feeds rarest-first until the budget runs out; the rest
    # become stubs. Whole feeds only.
    used = 0
    for pk in sorted(evictable, key=lambda p: _keep_rank(p, holder_counts, my_id)):
        size = bytes_of[pk]
        if used + size <= remaining:
            plan.keep.append(pk)
            used += size
        else:
            plan.evict.append(pk)

    # Fill whatever budget is left with the rarest feeds we don't yet fully
    # hold -- absent feeds, or ones a peer's digest shows are ahead of our
    # local copy. This is how a node actively helps preserve scarce roasts
    # rather than only reacting to what it happens to be handed.
    free = remaining - used
    keep_set = set(plan.keep)
    candidates = []
    for pk, kf in known.items():
        h = held.get(pk)
        if h is None or kf.latest_seq > h.latest_seq:
            candidates.append(pk)

    for pk in sorted(candidates, key=lambda p: _keep_rank(p, holder_counts, my_id)):
        if len(plan.acquire) >= MAX_ACQUIRE_PER_ROUND:
            break
        kf = known[pk]
        already = bytes_of.get(pk, 0)
        incremental = max(kf.total_bytes - already, 0)
        # A pinned or already-keep feed is always worth completing; an
        # evictable new feed only if it actually fits the free budget.
        if pk in keep_set or pk in pinned:
            plan.acquire.append(pk)
        elif incremental <= free:
            plan.acquire.append(pk)
            free -= incremental

    return plan


def cap_known_feeds(
    known: dict[str, KnownFeed],
    holder_counts: dict[str, int],
    held: set[str],
    pinned: set[str],
    *,
    limit: int = MAX_KNOWN_FEEDS,
) -> set[str]:
    """Which feed pubkeys to drop from the ledger when it grows past `limit`.

    Never a feed we hold or have pinned -- those are real, local, and cheap to
    keep a row for. Among the rest (stubs and never-fetched hints), the most
    replicated are the least worth remembering: if many peers hold a feed, its
    survival does not depend on our ledger row. Returns the set to forget;
    caller deletes those rows. Bounds the cap_peers-class growth vector on
    gossiped feed names.
    """
    protected = held | pinned
    droppable = [pk for pk in known if pk not in protected]
    if len(known) <= limit:
        return set()
    # Worst-to-keep first: most-replicated, then (tie) an arbitrary but stable
    # order by pubkey so the choice is deterministic.
    ranked = sorted(droppable, key=lambda p: (-holder_counts.get(p, 0), p))
    overflow = len(known) - limit
    return set(ranked[:overflow])
