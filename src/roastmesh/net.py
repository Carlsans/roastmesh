"""Peer connectivity over Iroh: bind a node, serve requests, sync with a peer.

The Iroh endpoint's own secret key IS the roastmesh feed identity (confirmed
empirically: `iroh.EndpointOptions(secret_key=...)` accepts the same raw
32-byte Ed25519 seed `roastmesh.identity` uses, and the resulting node id
equals the feed's public key hex exactly) -- so dialing a peer's NodeId and
verifying their feed are the same key, matching ARCHITECTURE.md's Core
Model literally ("the public key *is* the feed address *is* the
namespace"). This also means `conn.remote_id()` after connecting already IS
the authenticated feed pubkey -- no separate "who are you" request needed.

Wire protocol: JSON request ops over Iroh's bidirectional QUIC streams. No
manual length-prefixing -- `write_all()` + `finish()` on one side and
`read_to_end(cap)` on the other already frame exactly one message.

    {"op": "get_peers"}                            -> {"peers": [...]}
    {"op": "get_feed_meta", "since_seq": N}         -> {"entries": [...]}  (no blob_base64, ever)
    {"op": "get_feed", "since_seq": N, "limit": K}  -> {"entries": [...]}  (this node's own feed,
                                                        seq >= N, at most K entries, "limit"
                                                        omitted = unbounded)
    {"op": "get_profile"}                          -> {"profile": {...} | None}  (this node's own
                                                        signed profile.py profile, or None if it
                                                        has never set one)

`get_feed_meta` exists so a client can apply ARCHITECTURE.md's Abuse
Resistance quota checks (roastmesh.quota) against cheap metadata *before*
deciding how much content is worth fetching -- see sync_with_peer.

Scope: `get_feed`/`get_feed_meta`/`get_profile` only ever return the
responding peer's *own* feed/profile -- syncing with peer P replicates
exactly P's data, not a relay of everyone P knows about ("every peer
mirrors the entire corpus" is later work). `get_profile` is additive: an
un-upgraded peer answers `{"error": "unknown op 'get_profile'"}`, which
sync_with_peer treats as "this peer has no profile", never as a failure --
the existing three ops and their payloads are byte-for-byte unchanged.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import iroh

from roastmesh.feed import (
    _PUBKEY_RE,
    FeedEntry,
    FeedVerifyResult,
    blob_path_for,
    default_peer_feeds_root,
    feed_is_fully_held,
    held_feeds_digest,
    read_entries,
    verify_feed,
    write_received_entry,
)
from roastmesh.identity import Identity
from roastmesh.index import repository as repo
from roastmesh.index.db import connect
from roastmesh.index.ingest import ingest_feed
from roastmesh.lan_discovery import BEACON_INTERVAL_S, BEACON_PORT, run_beacon
from roastmesh.peers import (
    Peer,
    cap_peers,
    load_peers,
    peer_from_dict,
    prune_stale,
    save_peers,
    upsert_peer,
)
from roastmesh.profile import load_profile, verify_profile
from roastmesh.quota import QuotaCheckResult, QuotaLimits, check_feed_metadata
from roastmesh import replication
from roastmesh.wan_discovery import DHT_LOOKUP_INTERVAL_S, WAN_PORT, run_wan_discovery
from roastmesh.watch_folder import publish_new_files

ALPN = b"roastmesh/peer-sync/0"

# Verbose network logging, toggled by serve(debug=...) / `node serve --debug`.
# Read by the discovery/sync loops to print extra detail for a diagnostics log.
_DEBUG = False

# Peers are dropped after this long unseen, and the sweep runs this often.
# `peer prune` has existed since early on and nothing ever called it, so in
# practice the list only grew: measured on a real node at 764 entries, 746 of
# them learned through gossip and never once contacted. Gossip is worth having
# -- it is how a node finds anyone beyond its own two or three -- but a list
# that only accumulates turns every lookup and every gossip exchange into
# carrying other people's dead addresses around forever.
PEER_MAX_AGE_DAYS = 30.0
PEER_PRUNE_INTERVAL_S = 6 * 3600.0

# A dial has to be bounded, and the reason is not impatience.
#
# A ticket pins the addresses a peer had when it minted the ticket. Once those
# go stale the dial does not fail -- it sits there, for far longer than anyone
# will wait. That matters more than the delay: the fallback in sync_with_peer
# reconnects by identity alone in about two seconds, so a hang does not merely
# postpone success, it prevents a recovery that would have worked. Observed as
# `peer sync` producing no output whatsoever for 75 seconds, against a peer
# that was online and reachable throughout.
CONNECT_TIMEOUT_S = 20.0
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


@dataclass
class SyncReport:
    peer_pubkey_hex: str
    new_entry_count: int
    verify: FeedVerifyResult
    quota: QuotaCheckResult
    peers_known: int
    # The peer's own signed profile (profile.py), once verified against its
    # own pubkey and against `conn.remote_id()` -- or None if the peer has
    # no profile, answered `unknown op 'get_profile'` (an un-upgraded
    # node), or its profile failed verification. Never a third party's
    # profile: see sync_with_peer's docstring on why relaying is refused.
    profile: dict | None = None
    # The peer's own get_held_feeds digest -- every feed it holds with servable
    # bytes, as [{pubkey, latest_seq, entry_count, total_bytes}, ...]. Empty for
    # a peer too old to know the op. The caller records this peer as a holder of
    # each and merges the digest into the replication ledger. Never trusted for
    # budget: the bytes are re-measured after any feed is actually pulled.
    held_feeds: list = field(default_factory=list)
    # Third-party feeds (pubkey hex) this sync actually pulled into their
    # mirror dirs because `also_pull` asked for them and this peer held them --
    # the caller verifies + ingests each, exactly like the peer's own feed.
    pulled_feeds: list = field(default_factory=list)


async def bind_endpoint(identity: Identity, *, alpns: list[bytes] | None = None, relay: bool = True) -> iroh.Endpoint:
    kwargs = {
        "preset": iroh.preset_n0(),
        "secret_key": bytes.fromhex(identity.secret_key_hex),
    }
    if alpns is not None:
        kwargs["alpns"] = alpns
    if not relay:
        kwargs["relay_mode"] = iroh.RelayMode.disabled()
    return await iroh.Endpoint.bind(iroh.EndpointOptions(**kwargs))


async def _send_message(bi, message: dict) -> None:
    send = bi.send()
    await send.write_all(json.dumps(message).encode())
    await send.finish()


async def _recv_message(bi) -> dict:
    raw = await bi.recv().read_to_end(MAX_MESSAGE_BYTES)
    return json.loads(raw)


async def _request(conn, message: dict) -> dict:
    bi = await conn.open_bi()
    await _send_message(bi, message)
    return await _recv_message(bi)


def _entry_to_wire(feed_dir: Path, entry: FeedEntry) -> dict:
    blob = blob_path_for(feed_dir, entry).read_bytes()
    return {**asdict(entry), "blob_base64": base64.b64encode(blob).decode("ascii")}


def _entry_from_wire(wire: dict) -> tuple[FeedEntry, bytes]:
    wire = dict(wire)
    blob = base64.b64decode(wire.pop("blob_base64"))
    return FeedEntry(**wire), blob


def _feed_dir_for_request(
    request: dict, feed_dir: Path, peer_feeds_root: Path | None, own_pubkey_hex: str | None
) -> tuple[Path | None, str | None]:
    """Which feed a get_feed/get_feed_meta request targets: this node's own
    feed when no `pubkey` is given (byte-for-byte the original behavior), or a
    mirrored third-party feed under peer_feeds_root when one is -- how a node
    re-serves a feed it did not author (replication.py).

    The pubkey is validated against _PUBKEY_RE *before* it becomes a path
    component (the same guard hello.decode_hello / feed._init_feed_dir use):
    an unauthenticated request must never be able to name `../` its way out of
    peer_feeds_root. A well-formed pubkey we simply don't hold returns (None,
    None) -- 'no such feed here', not an error -- so the caller just tries
    another holder. Returns (dir, error_message)."""
    pubkey = request.get("pubkey")
    if pubkey is None or (own_pubkey_hex is not None and pubkey == own_pubkey_hex):
        return feed_dir, None
    if not isinstance(pubkey, str) or not _PUBKEY_RE.match(pubkey):
        return None, "invalid pubkey"
    if peer_feeds_root is None:
        return None, None
    mirror = Path(peer_feeds_root) / pubkey
    # Only ever serve a feed whose bytes we actually hold -- never fetch-through
    # on a requester's behalf (no amplification, no open relay to arbitrary
    # third parties beyond what we already store).
    if not feed_is_fully_held(mirror):
        return None, None
    return mirror, None


def _build_response(
    request: dict, feed_dir: Path, peers_path: Path, profile_path: Path | None = None,
    peer_feeds_root: Path | None = None, own_pubkey_hex: str | None = None,
) -> dict:
    op = request.get("op")
    if op == "get_peers":
        return {"peers": [asdict(p) for p in load_peers(peers_path)]}
    if op == "get_held_feeds":
        # A cheap digest of every feed we hold with servable bytes -- our own
        # plus every fully-held mirror. This is how holdings gossip so a peer
        # can discover, and later pull, feeds it never synced directly.
        return {"feeds": held_feeds_digest(feed_dir, own_pubkey_hex or "", peer_feeds_root
                                           or default_peer_feeds_root())}
    if op == "get_feed_meta":
        target, error = _feed_dir_for_request(request, feed_dir, peer_feeds_root, own_pubkey_hex)
        if error is not None:
            return {"error": error}
        if target is None:
            return {"entries": []}
        since_seq = int(request.get("since_seq", 0))
        entries = [e for e in read_entries(target) if e.seq >= since_seq]
        return {"entries": [asdict(e) for e in entries]}
    if op == "get_feed":
        target, error = _feed_dir_for_request(request, feed_dir, peer_feeds_root, own_pubkey_hex)
        if error is not None:
            return {"error": error}
        if target is None:
            return {"entries": []}
        since_seq = int(request.get("since_seq", 0))
        entries = [e for e in read_entries(target) if e.seq >= since_seq]
        limit = request.get("limit")
        if limit is not None:
            entries = entries[:int(limit)]
        return {"entries": [_entry_to_wire(target, e) for e in entries]}
    if op == "get_profile":
        # A peer may only ever be served ITS OWN profile -- there is no
        # "profile of pubkey X" lookup here, deliberately: this node has no
        # way to know that a claimed third-party profile wasn't tampered
        # with in transit by whoever is answering. sync_with_peer enforces
        # the other half of that (profile["pubkey"] == conn.remote_id()).
        own_profile = load_profile(profile_path)
        return {"profile": own_profile.to_dict() if own_profile else None}
    return {"error": f"unknown op {op!r}"}


async def _handle_request(bi, feed_dir: Path, peers_path: Path, profile_path: Path | None = None,
                          peer_feeds_root: Path | None = None, own_pubkey_hex: str | None = None) -> None:
    try:
        request = await _recv_message(bi)
        response = _build_response(request, feed_dir, peers_path, profile_path,
                                   peer_feeds_root, own_pubkey_hex)
    except Exception as exc:
        response = {"error": str(exc)}
    try:
        await _send_message(bi, response)
    except Exception:
        pass  # peer likely disconnected mid-response; nothing more to do


async def _handle_connection(
    incoming, feed_dir: Path, peers_path: Path, profile_path: Path | None = None,
    peer_feeds_root: Path | None = None, own_pubkey_hex: str | None = None,
    devices_dir: Path | None = None, device_sync_state_path: Path | None = None,
) -> None:
    """Accept one incoming connection and route it by ALPN.

    serve() now binds a single endpoint that answers three protocols (see
    its own docstring and the module docstring's "Verified facts"): the
    public feed's peer-sync (`ALPN`, unchanged below -- every existing
    behavior for it is untouched), the private folder mirror's sync
    (device_sync.SYNC_ALPN, trusted-devices-only), and device pairing
    (device_sync.PAIR_ALPN). `conn.alpn()` is only known once the handshake
    itself has completed (after `accepting.connect()`), which is why the
    routing decision happens here and not earlier.
    """
    from roastmesh import device_sync  # local: device_sync.py imports net.py right back

    try:
        accepting = await incoming.accept()
        conn = await accepting.connect()
    except Exception:
        return

    alpn = conn.alpn()

    if alpn == device_sync.SYNC_ALPN:
        if devices_dir is None or device_sync_state_path is None:
            # This node isn't configured for device sync at all (e.g.
            # enable_device_sync=False) -- refuse outright rather than
            # silently answering with no folder to serve.
            _close_quietly(conn)
            return
        await device_sync.handle_sync_connection(conn, devices_dir, device_sync_state_path)
        return

    if alpn == device_sync.PAIR_ALPN:
        # Real pairing is driven end-to-end by device_sync.pair_over_lan's
        # own dedicated endpoint on both sides -- see that function's
        # docstring for why a short-lived, human-attended exchange binds its
        # own connection rather than sharing this long-running node's. A
        # PAIR_ALPN dial that lands here regardless (e.g. an old ticket, or
        # a misdirected one) still gets a clean, immediate close instead of
        # an unanswered hang.
        _close_quietly(conn)
        return

    while True:
        try:
            bi = await conn.accept_bi()
        except Exception:
            return  # connection closed
        asyncio.create_task(_handle_request(bi, feed_dir, peers_path, profile_path,
                                            peer_feeds_root, own_pubkey_hex))


def _close_quietly(conn) -> None:
    try:
        conn.close(0, b"")
    except Exception:
        pass


# How often serve() re-enforces the disk budget and refreshes the acquire set.
# Matched to the WAN discovery retry cadence -- replication rides on the same
# syncs that discovery already drives, so re-planning much more often than new
# peers arrive would just churn. A node fills spare budget over many rounds.
REPLICATION_INTERVAL_S = 15 * 60.0


def _truncate_mirror_to(mirror: Path, valid_count: int) -> None:
    """Delete every entry at or after `valid_count` (and any blob left with no
    remaining entry pointing at it), leaving only the verified prefix on disk.
    read_entries is sorted by filename, which is the seq, so entries[valid_count:]
    is exactly the unverified tail."""
    entries = read_entries(mirror)
    kept_hashes = {e.content_sha256 for e in entries[:valid_count]}
    for e in entries[valid_count:]:
        (mirror / "entries" / f"{e.seq:08d}.json").unlink(missing_ok=True)
        if e.content_sha256 not in kept_hashes:
            blob_path_for(mirror, e).unlink(missing_ok=True)


async def _pull_third_party_feed(conn_iroh, target_pubkey_hex: str, peer_feeds_root: Path,
                                 limits: QuotaLimits) -> bool:
    """Pull a feed we did NOT author from a peer that holds it, into that
    feed's own mirror dir, verifying the whole chain against
    target_pubkey_hex. Returns True if new verified entries landed.

    Quota-checked on metadata first (a hostile holder can't make us store an
    oversized third-party feed), and re-verified after: forgery is impossible
    because the signature is the author's, not the server's. A feed that fails
    verification from an empty start is deleted wholesale -- there's no local
    investment to protect, and an unverifiable prefix left on disk would block
    a good holder from supplying it cleanly next round.
    """
    mirror = Path(peer_feeds_root) / target_pubkey_hex
    existing = read_entries(mirror)
    since = len(existing)
    meta = await _request(conn_iroh, {"op": "get_feed_meta", "since_seq": since, "pubkey": target_pubkey_hex})
    if "error" in meta:
        return False
    candidates = [FeedEntry(**d) for d in meta.get("entries", [])]
    if not candidates:
        return False
    quota = check_feed_metadata(existing, candidates, limits)
    if quota.allowed_count <= 0:
        return False
    feed = await _request(conn_iroh, {"op": "get_feed", "since_seq": since,
                                      "limit": quota.allowed_count, "pubkey": target_pubkey_hex})
    if "error" in feed:
        return False
    wrote = 0
    for wire in feed.get("entries", []):
        entry, blob = _entry_from_wire(wire)
        write_received_entry(mirror, target_pubkey_hex, entry, blob)
        wrote += 1
    if wrote == 0:
        return False
    result = verify_feed(mirror, expected_pubkey_hex=target_pubkey_hex)
    total_on_disk = len(read_entries(mirror))
    if result.valid_count < total_on_disk:
        # Keep only the verified prefix. A malicious holder can serve a real
        # prefix followed by a forged tail entry: verify_feed rejects the tail
        # (so it never reaches the index), but left on disk that tail both lets
        # us re-serve unverifiable bytes AND wedges since_seq -- we would think
        # we already hold that seq and never pull the feed's real later entries
        # from a good holder. Truncating to the verified prefix closes both.
        _truncate_mirror_to(mirror, result.valid_count)
    if result.valid_count == 0:
        # Nothing verified at all -- drop the dir so a good holder can supply
        # it fresh (there is no valid prefix to protect).
        shutil.rmtree(mirror, ignore_errors=True)
        return False
    return result.valid_count > since


def record_sync_replication(conn, report: "SyncReport", peer_feeds_root: Path) -> None:
    """Fold a sync's replication data into the ledger.

    Records the peer as a *first-hand* holder of every feed it advertised --
    we only ever record a holder we reached ourselves, never a third party a
    peer merely names, which is what keeps a stranger from inflating a rare
    feed's apparent replication to get it evicted (ARCHITECTURE.md's local-only
    trust). Then verifies + ingests any third-party feeds this sync pulled and
    marks their blobs local. Holder rows per feed are bounded (cap_feed_holders).
    """
    peer = report.peer_pubkey_hex
    for d in report.held_feeds:
        fpk = d.get("pubkey")
        if not isinstance(fpk, str) or not _PUBKEY_RE.match(fpk):
            continue
        repo.upsert_known_feed(conn, fpk, latest_seq=d.get("latest_seq"),
                               total_bytes=d.get("total_bytes"), entry_count=d.get("entry_count"))
        repo.record_feed_holder(conn, fpk, peer, d.get("latest_seq"))
        repo.cap_feed_holders(conn, fpk, replication.MAX_HOLDERS_PER_FEED)
    for fpk in report.pulled_feeds:
        if not _PUBKEY_RE.match(fpk):
            continue
        mirror = Path(peer_feeds_root) / fpk
        ingest_feed(conn, mirror, expected_pubkey_hex=fpk)
        repo.set_blobs_local(conn, fpk, True)
        repo.upsert_known_feed(conn, fpk, held_local=True)


def _pinned_pubkeys(conn, peers_path: Path, own_pubkey_hex: str) -> set[str]:
    """Feeds that must never be evicted: our own, every manually-added or
    bootstrap peer (a deliberate choice by the user), and every favorited
    author. These are kept and still counted against the budget."""
    pinned = {own_pubkey_hex}
    for peer in load_peers(peers_path):
        if peer.added_via in ("manual", "bootstrap") and peer.feed_pubkey_hex:
            pinned.add(peer.feed_pubkey_hex)
    for row in conn.execute("SELECT pubkey_hex FROM users WHERE is_favorite = 1"):
        pinned.add(row["pubkey_hex"])
    return pinned


def _evict_feed_to_stub(conn, peer_feeds_root: Path, feed_pubkey: str) -> None:
    """Reclaim a feed's disk: delete its mirror dir (entries + blobs) and flag
    each of its roasts not-local, keeping the index rows as searchable stubs.
    On-demand fetch re-materializes the bytes from a holder later."""
    mirror = Path(peer_feeds_root) / feed_pubkey
    shutil.rmtree(mirror, ignore_errors=True)
    repo.set_blobs_local(conn, feed_pubkey, False)
    print(f"replication: evicted feed {feed_pubkey[:16]}... to a stub (over budget)", flush=True)


async def _replication_loop(feed_dir: Path, own_pubkey_hex: str, peer_feeds_root: Path,
                            peers_path: Path, db_path: Path, budget_bytes: int,
                            acquire_state: list) -> None:
    """Enforce the disk budget and steer acquisition, periodically.

    Measures what we hold on disk, loads the ledger, runs
    replication.plan_retention, evicts over-budget feeds to stubs, bounds the
    ledger's gossip-grown growth, and publishes the rarest wanted feeds into
    `acquire_state` so the discovery syncs pull them (sync_with_peer's
    also_pull). Budget accounting is measured bytes only, never a declaration.
    Runs in a thread (synchronous DB/file work) so it never blocks the endpoint.
    """
    own_id = bytes.fromhex(own_pubkey_hex)

    def _work() -> list:
        conn = connect(db_path)
        try:
            digest = held_feeds_digest(feed_dir, own_pubkey_hex, peer_feeds_root)
            local = [replication.FeedHolding(d["pubkey"], d["entry_count"],
                                             d["total_bytes"], d["latest_seq"]) for d in digest]
            for d in digest:
                repo.upsert_known_feed(conn, d["pubkey"], latest_seq=d["latest_seq"],
                                       total_bytes=d["total_bytes"], entry_count=d["entry_count"],
                                       held_local=True)
            repo.set_feed_pinned(conn, own_pubkey_hex, True)
            pinned = _pinned_pubkeys(conn, peers_path, own_pubkey_hex)
            for pk in pinned:
                repo.upsert_known_feed(conn, pk, pinned=True)
            known_rows = repo.load_known_feeds(conn)
            known = {r["feed_pubkey"]: replication.KnownFeed(
                        r["feed_pubkey"], r["latest_seq"] or 0,
                        r["total_bytes"] or 0, r["entry_count"] or 0)
                     for r in known_rows}
            holder_counts = repo.feed_holder_counts(conn, exclude_holder=own_pubkey_hex)
            plan = replication.plan_retention(local, known, holder_counts, pinned, own_id, budget_bytes)
            for pk in plan.evict:
                _evict_feed_to_stub(conn, peer_feeds_root, pk)
            held = repo.held_feed_pubkeys(conn)
            drop = replication.cap_known_feeds(known, holder_counts, held, pinned)
            if drop:
                repo.delete_known_feeds(conn, drop)
            return plan.acquire
        finally:
            conn.close()

    while True:
        try:
            acquire = await asyncio.to_thread(_work)
            acquire_state[:] = acquire
        except Exception as exc:  # noqa: BLE001 -- housekeeping must not kill serve()
            print(f"replication: pass failed: {exc!r}", flush=True)
        await asyncio.sleep(REPLICATION_INTERVAL_S)


def persist_peer_profile(conn, profile: dict) -> None:
    """Write a signature-verified peer profile (sync_with_peer has already
    checked verify_profile and the pubkey/conn.remote_id match before this
    is ever called) into the local index: the `users` row itself, plus
    every pubkey in its `likes` list as a `user_likes` row attributed to
    the profile's own pubkey as liker -- the only two things a synced
    profile can ever produce (repository.py's `user_likes` invariant).
    Shared by the LAN/WAN auto-sync path and the CLI's `peer sync`."""
    pubkey = profile["pubkey"]
    repo.upsert_user_from_profile(
        conn,
        pubkey_hex=pubkey,
        display_name=profile.get("name"),
        machine_key=profile.get("machine_key"),
        machine_display=profile.get("machine_display"),
        profile_updated_at=profile.get("updated_at"),
    )
    for liked_pubkey in profile.get("likes") or []:
        repo.add_user_like(conn, pubkey, liked_pubkey)


def _index_is_behind_mirror(conn, peer_pubkey_hex: str, mirror_dir: Path) -> bool:
    """True when a peer's mirrored feed holds entries the index doesn't.

    Cheap (one COUNT against an indexed column) and only ever used to decide
    whether to re-run an ingest that is itself idempotent, so a false positive
    costs a little work and a false negative costs correctness -- which is the
    right way round.
    """
    try:
        mirrored = len(read_entries(mirror_dir))
    except OSError:
        return False
    if mirrored == 0:
        return False
    indexed = conn.execute(
        "SELECT COUNT(*) FROM sources WHERE author_pubkey = ?", (peer_pubkey_hex,)
    ).fetchone()[0]
    return indexed < mirrored


async def _auto_sync_discovered_peer(
    peer_pubkey_hex: str,
    peer_ticket: str,
    *,
    identity: Identity,
    peer_feeds_root: Path,
    peers_path: Path,
    db_path: Path | None,
    relay: bool,
    source: str = "lan",
    also_pull: list[str] | None = None,
    known_tickets: dict[str, str] | None = None,
    devices_dir: Path | None = None,
    device_sync_state_path: Path | None = None,
) -> None:
    """Shared by LAN and internet (DHT) auto-discovery -- `source` (used
    both as a log-line prefix and as the peer's `added_via` tag) is the
    only thing that differs between the two callers. `also_pull` is the
    replication acquire set: third-party feeds to pull from this peer if it
    holds them (replication.py).

    `known_tickets`, if given, is updated with this peer's ticket
    unconditionally, before anything below can fail -- it is serve()'s "who
    did we last hear from and how do we reach them" memory, read by
    `_device_watch_loop` to push a local device-sync change out immediately
    to a currently-reachable paired device. `devices_dir`/
    `device_sync_state_path`, if given, are what let this function ALSO
    reconcile the private folder mirror with this peer when it turns out to
    be one of our own paired devices (devices.is_trusted) -- this is how a
    device that went offline catches up the moment it's discovered again,
    independent of whether the unrelated public-feed sync below succeeds.
    """
    if known_tickets is not None:
        known_tickets[peer_pubkey_hex] = peer_ticket

    print(f"{source}: discovered {peer_pubkey_hex[:16]}..., syncing", flush=True)
    try:
        report = await sync_with_peer(
            peer_ticket, identity, peer_feeds_root, peers_path, relay=relay, added_via=source,
            also_pull=also_pull,
        )
    except Exception as exc:  # noqa: BLE001 -- a bad/unreachable peer hint shouldn't kill serve()
        print(f"{source}: sync with {peer_pubkey_hex[:16]}... failed: {exc!r}", flush=True)
    else:
        verify_msg = "OK" if report.verify.ok else f"INVALID: {report.verify.error}"
        print(f"{source}: synced with {peer_pubkey_hex[:16]}...: {report.new_entry_count} new entries, feed {verify_msg}",
              flush=True)
        if _DEBUG:
            print(f"{source}: debug: peer advertised {len(report.held_feeds)} feed(s), "
                  f"pulled {len(report.pulled_feeds)}, quota held_back={report.quota.held_back}, "
                  f"peers_known={report.peers_known}", flush=True)

        # Two independent things can each need the DB, and neither implies the
        # other: new feed entries to ingest, and a profile to persist so this
        # peer gets a name/machine even on a sync that pulled nothing new (the
        # trap this comment exists to flag -- a peer who has already published
        # everything they ever will still deserves to stop showing up as a bare
        # pubkey). So the connection is opened whenever db_path is given at
        # all, not gated on new_entry_count.
        if db_path is not None:
            conn = connect(db_path)
            try:
                mirror_dir = Path(peer_feeds_root) / peer_pubkey_hex
                # "Nothing new arrived" is not the same as "nothing to ingest".
                # A mirror can hold entries the index does not: every feed
                # published before the roastnet -> roastmesh rename failed
                # verification on arrival, so its entries were mirrored to disk
                # and then dropped. Upgrading fixed the verification, but this
                # path still asked only "did new entries arrive?" -- the answer
                # was 0, ingest was skipped, and those roasts stayed invisible
                # forever unless the user happened to run `peer sync` by hand.
                # Comparing what the mirror holds against what the index has for
                # that author costs one COUNT and makes the upgrade self-healing.
                if report.new_entry_count > 0 or _index_is_behind_mirror(conn, peer_pubkey_hex, mirror_dir):
                    ingest_feed(conn, mirror_dir, expected_pubkey_hex=peer_pubkey_hex)
                if report.profile is not None:
                    persist_peer_profile(conn, report.profile)
                # Replication: note who holds what, and ingest any third-party
                # feeds this sync pulled (marking their blobs local).
                record_sync_replication(conn, report, Path(peer_feeds_root))
                if report.pulled_feeds:
                    print(f"{source}: replicated {len(report.pulled_feeds)} feed(s) held by others",
                          flush=True)
            finally:
                conn.close()

    if devices_dir is not None and device_sync_state_path is not None:
        # Local import: device_sync.py imports net.py right back.
        from roastmesh import device_sync
        from roastmesh import devices as devices_mod

        if devices_mod.is_trusted(peer_pubkey_hex):
            try:
                await device_sync.reconcile_with_device(
                    peer_ticket, identity, devices_dir, device_sync_state_path, relay=relay,
                )
            except Exception as exc:  # noqa: BLE001 -- catch-up must not kill serve()
                print(f"{source}: device-sync catch-up with {peer_pubkey_hex[:16]}... failed: {exc!r}",
                      flush=True)


async def _prune_peers_loop(peers_path) -> None:
    """Drop peers nobody has heard from in a month, periodically.

    Deliberately load-modify-save rather than anything cleverer: a peer added
    by a sync in the same instant could be lost, and that is fine -- gossip
    re-learns it within a round. Holding a lock across a network sync to avoid
    that would trade a self-healing rarity for a real stall.
    """
    while True:
        try:
            peers = load_peers(peers_path)
            kept = prune_stale(peers, max_age_days=PEER_MAX_AGE_DAYS)
            if len(kept) != len(peers):
                save_peers(kept, peers_path)
                print(f"peers: dropped {len(peers) - len(kept)} not seen in "
                      f"{PEER_MAX_AGE_DAYS:.0f} days, {len(kept)} remaining", flush=True)
        except Exception as exc:  # noqa: BLE001 -- housekeeping must not kill serve()
            print(f"peers: prune failed: {exc!r}", flush=True)
        await asyncio.sleep(PEER_PRUNE_INTERVAL_S)


async def _refresh_index_if_needed(db_path: Path) -> None:
    """A one-shot, version-gated refresh of everything the index already
    knows about, run once at the start of every `serve()` -- see
    cli.py's `refresh` command for the full reasoning. Runs in a thread
    (it's synchronous DB/file work) so it never blocks the endpoint from
    binding or peers from connecting while it runs; on a large corpus
    that could take a moment, but it's a no-op after the first run for a
    given version, since index_meta records that it already happened."""
    import roastmesh
    from roastmesh.index.db import get_meta, set_meta
    from roastmesh.index.ingest import refresh_known_sources

    def _work() -> None:
        conn = connect(db_path)
        try:
            current = roastmesh.__version__
            if get_meta(conn, "refreshed_by_version") == current:
                return
            print(f"refresh: updating search index for v{current}...", flush=True)
            results = refresh_known_sources(conn)
            refreshed = sum(1 for r in results if r.error is None)
            print(f"refresh: refreshed {refreshed} roast(s) for v{current}", flush=True)
            set_meta(conn, "refreshed_by_version", current)
        finally:
            conn.close()

    await asyncio.to_thread(_work)


async def _watch_publish_loop(
    feed_dir: Path, identity: Identity, watch_dir: Path, interval_s: float, db_path: Path | None,
) -> None:
    watch_dir = Path(watch_dir)
    try:
        watch_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"watch: could not create {watch_dir}: {exc!r}", flush=True)
        return
    # Persists across every tick of this loop for as long as `serve` runs --
    # see publish_new_files' skip_cache docstring for why this matters.
    skip_cache: dict[Path, tuple[float, int]] = {}
    while True:
        try:
            published = await asyncio.to_thread(
                publish_new_files, feed_dir, identity, watch_dir, db_path=db_path,
                skip_cache=skip_cache,
            )
            for entry in published:
                print(f"watch: published entry {entry.seq} ({entry.content_sha256[:12]}...) "
                      f"from {watch_dir}", flush=True)
        except Exception as exc:  # noqa: BLE001 -- a bad file in the folder shouldn't kill serve()
            print(f"watch: error scanning {watch_dir}: {exc!r}", flush=True)
        await asyncio.sleep(interval_s)


DEVICE_WATCH_INTERVAL_S = 5.0


async def _device_watch_loop(
    devices_dir: Path, state_path: Path, identity: Identity, relay: bool,
    known_tickets: dict[str, str], interval_s: float = DEVICE_WATCH_INTERVAL_S,
) -> None:
    """Poll the private device-sync folder on a timer (device_sync.scan_folder's
    cheap mtime/size fingerprint pass -- see its own docstring) and, the
    moment a change is noticed, push it out right away to every paired
    device this node currently knows how to reach -- this is what makes
    "drop a file on one device" show up on the other "within seconds"
    rather than waiting for the next scheduled discovery round.

    The sync db is saved on every pass regardless of whether anything
    changed, so a SYNC_ALPN request served concurrently (net._handle_connection)
    always sees state at least as fresh as this loop's last pass.

    A folder with no paired device reachable right now is not an error --
    this loop's job is "notice and try immediately", not "guarantee
    delivery this instant". A device that's offline (not in `known_tickets`)
    catches up the moment it's next discovered instead
    (_auto_sync_discovered_peer's own device-sync catch-up, above).
    """
    # Local import: device_sync.py imports net.py right back.
    from roastmesh import device_sync
    from roastmesh import devices as devices_mod

    while True:
        try:
            prev = device_sync.load_state(state_path)
            manifest = device_sync.scan_folder(devices_dir, prev)
            changed = manifest != prev
            device_sync.save_state(manifest, state_path)
            if changed:
                for device in devices_mod.load_devices():
                    ticket = known_tickets.get(device.pubkey)
                    if ticket is None:
                        continue  # not currently known-reachable -- catches up on discovery
                    try:
                        await device_sync.reconcile_with_device(
                            ticket, identity, devices_dir, state_path, relay=relay,
                        )
                    except Exception as exc:  # noqa: BLE001 -- one unreachable device must not kill the loop
                        print(f"device-sync: push to {device.name!r} failed: {exc!r}", flush=True)
        except Exception as exc:  # noqa: BLE001 -- housekeeping must not kill serve()
            print(f"device-sync: watch loop error: {exc!r}", flush=True)
        await asyncio.sleep(interval_s)


def _discovery_is_offline() -> bool:
    """True when ROASTMESH_DISCOVERY_OFFLINE is set to something truthy.

    A node that must not touch the network at all. This exists because the
    test suite was quietly polluting the real one: every GUI test builds a
    RoastmeshApp, which auto-starts `node serve` with a throwaway identity
    from an isolated HOME, and that node beaconed on the real LAN port and
    -- since wan_discovery_enabled defaults to True -- announced itself on
    the real public BitTorrent DHT before being torn down seconds later.

    Measured, not guessed: sniffing UDP 41888 during one `pytest
    tests/test_gui.py` run showed the two genuine nodes beaconing every 5s
    plus four one-shot pubkeys that existed only for the length of a test,
    and this machine's peers.json grew by exactly those four. It holds 876
    peers of which two have ever published anything; 606 arrived this way.
    Each one was also announced to the global swarm, so every other
    roastmesh user was handed dead peers to try to reach, and it burned
    this IP's share of the public DHT's rate limits.

    Also genuinely useful outside tests: a node that should serve and sync
    only with peers you hand it, with no broadcast and no DHT presence.
    """
    return os.environ.get("ROASTMESH_DISCOVERY_OFFLINE", "").strip().lower() not in ("", "0", "false", "no")


async def serve(
    identity: Identity,
    feed_dir: Path,
    peers_path: Path,
    *,
    relay: bool = True,
    ready_callback: Callable[[str], None] | None = None,
    db_path: Path | None = None,
    peer_feeds_root: Path | None = None,
    enable_lan_discovery: bool = True,
    lan_discovery_port: int = BEACON_PORT,
    lan_discovery_interval_s: float = BEACON_INTERVAL_S,
    enable_wan_discovery: bool = False,
    wan_discovery_port: int = WAN_PORT,
    wan_public_port: int | None = None,
    wan_auto_port: bool = False,
    wan_discovery_interval_s: float = DHT_LOOKUP_INTERVAL_S,
    profile_path: Path | None = None,
    publish_watch_dir: Path | None = None,
    publish_watch_interval_s: float = 10.0,
    replicate: bool = True,
    replication_budget: int = replication.DEFAULT_REPLICATION_BUDGET,
    debug: bool = False,
    enable_device_sync: bool = True,
    devices_dir: Path | None = None,
    device_sync_state_path: Path | None = None,
) -> None:
    """Bind a node and serve get_peers/get_feed requests forever.

    If `enable_lan_discovery`, also broadcasts/listens for other roastmesh
    nodes on the local network (lan_discovery.run_beacon) and automatically
    syncs with any it finds -- no manual ticket-pasting needed for two
    machines on the same LAN.

    If `enable_wan_discovery`, does the same thing but over the public
    internet, via the real BitTorrent Mainline DHT (wan_discovery) instead
    of a local broadcast -- opt-in, since it makes this node's public
    address visible to anyone else looking at the same DHT swarm.

    Either way, `db_path`, if given, makes those automatic syncs also land
    in the local search index (same verify-then-ingest pipeline
    `peer sync` already uses for manual syncs).

    If `publish_watch_dir` is given, any `.alog` file placed there is
    automatically appended to this feed (watch_folder.publish_new_files) --
    "sharing" a roast becomes "drop the file in the folder", no publish
    command needed, for as long as this node is serving.

    If `db_path` is given, this also runs a one-time, version-gated
    refresh of every already-known roast's derived fields on startup (see
    cli.py's `refresh` command) -- so updating roastmesh and reopening it
    is enough to fix entries that look stale (an old roast_type, a
    missing title) because they were indexed by an older version, without
    a manual reindex and without losing hidden status or "my own roasts"
    tagging the way wiping the database would.

    `profile_path` is which signed profile (profile.py) this node answers
    `get_profile` requests with -- None (the default) means "whatever's at
    profile.default_profile_path()", i.e. this identity's own, real profile.
    Tests point it elsewhere so a profile fixture never touches the real
    user's config directory.

    Setting ROASTMESH_DISCOVERY_OFFLINE=1 forces both discovery mechanisms
    off regardless of what the caller asked for. See _discovery_is_offline.

    `enable_device_sync` (on by default) is the private folder mirror
    between this identity's own paired devices (device_sync.py) -- a
    completely separate trust boundary and protocol from everything above:
    public-feed peers are never involved, and nothing here is ever
    published to the public feed. The endpoint always advertises
    device_sync.SYNC_ALPN/PAIR_ALPN (see net._handle_connection's own
    "Verified facts": one endpoint, three ALPNs, branched by conn.alpn())
    regardless of this flag, so a dial to either gets a clean, immediate
    close rather than a transport-level failure; `enable_device_sync=False`
    is what makes that close unconditional, i.e. this node never actually
    serves a SYNC_ALPN request or starts device-sync activity of its own.
    `devices_dir`/`device_sync_state_path` default to
    paths.default_devices_dir()/paths.device_sync_state_path() when not
    given. The background watch loop that pushes a local change out
    immediately, and the reconcile-on-discovery catch-up, both additionally
    only start when `roastmesh.devices.load_devices()` is non-empty
    (nothing paired yet -- no reason to poll a folder or dial anyone) and
    when discovery isn't forced offline (ROASTMESH_DISCOVERY_OFFLINE -- see
    _discovery_is_offline; with discovery off there is no peer to reach
    anyway).
    """
    if _discovery_is_offline():
        # Only the *shared* channels are closed: the production LAN beacon
        # port, and the internet DHT (of which there is only one, whatever
        # local port is used). A caller that asked for LAN discovery on its
        # own port is talking to nobody but itself and is left alone -- which
        # is what lets the discovery tests keep proving discovery works while
        # the suite as a whole stays off the real network.
        muted = []
        if enable_lan_discovery and lan_discovery_port == BEACON_PORT:
            enable_lan_discovery = False
            muted.append(f"LAN beacon on the shared port {BEACON_PORT}")
        if enable_wan_discovery:
            enable_wan_discovery = False
            muted.append("internet (DHT) discovery")
        if muted:
            print(f"serve: ROASTMESH_DISCOVERY_OFFLINE is set -- disabled {' and '.join(muted)}",
                  flush=True)

    global _DEBUG
    _DEBUG = debug
    if debug:
        print("serve: debug logging enabled -- verbose discovery/sync detail follows", flush=True)

    # Local import: device_sync.py imports net.py right back (for
    # bind_endpoint/dial_with_fallback/_send_message/_recv_message).
    from roastmesh import device_sync
    from roastmesh import devices as devices_mod

    ep = await bind_endpoint(identity, alpns=[ALPN, device_sync.PAIR_ALPN, device_sync.SYNC_ALPN], relay=relay)
    if relay:
        # Wait for a home relay before minting the ticket. Immediately after
        # bind, `ep.addr()` knows only local interfaces -- measured here:
        #
        #   t=0s  addrs=[10.17.204.35, 172.17.0.1, ..., 192.168.0.222]   (no relay)
        #   t=3s  relay=https://use1-1.relay.n0.iroh.link./  addrs=[..., <public ip>]
        #
        # Minting at t=0 published a ticket containing nothing but VPN, Docker
        # and LAN addresses: unroutable from anywhere else, so a peer that
        # received it over the wire had no path to dial back. That is invisible
        # locally (same-machine dials succeed on those very addresses) and only
        # bites real internet peers.
        try:
            await asyncio.wait_for(ep.online(), timeout=15)
        except Exception:  # noqa: BLE001 -- no relay is a degraded state, not a fatal one
            print("serve: no relay yet; the ticket may only be reachable on this network",
                  flush=True)
    ticket = str(iroh.EndpointTicket.from_addr(ep.addr()))
    if ready_callback:
        ready_callback(ticket)
    else:
        # explicit flush: stdout is block-buffered (not line-buffered) once
        # it's not a TTY -- e.g. redirected to a log file or piped, which is
        # exactly how a long-running `node serve` is normally run. Without
        # this the ticket an operator needs immediately can sit unflushed
        # in the buffer for the process's entire lifetime.
        print(f"listening as {identity.public_key_hex[:16]}...")
        print(f"ticket: {ticket}", flush=True)

    resolved_peer_feeds_root = peer_feeds_root or default_peer_feeds_root()
    # None (device sync fully disabled) unless enable_device_sync -- passed
    # straight to _handle_connection, whose SYNC_ALPN branch already treats
    # "no folder configured" as "refuse", so False here needs no separate
    # gate at that call site.
    resolved_devices_dir = None
    resolved_device_sync_state_path = None
    if enable_device_sync:
        from roastmesh.paths import default_devices_dir, device_sync_state_path as default_device_sync_state_path
        resolved_devices_dir = Path(devices_dir) if devices_dir is not None else default_devices_dir()
        resolved_device_sync_state_path = Path(device_sync_state_path) if device_sync_state_path is not None \
            else default_device_sync_state_path()

    # The replication acquire set, refreshed each pass by _replication_loop and
    # read by the discovery syncs: the rarest wanted feeds to pull opportunistically
    # from whichever peer we next talk to that holds them. A plain list mutated
    # in place so the closures below always see the latest set.
    acquire_state: list[str] = []

    # Which ticket last reached us for a given pubkey, from EITHER discovery
    # mechanism -- device_sync's own reachability memory (a dict mutated in
    # place, same pattern as acquire_state above), read by _device_watch_loop
    # to push a local change to a currently-reachable paired device right
    # away instead of waiting for that device to be rediscovered.
    known_device_tickets: dict[str, str] = {}

    # Device sync only ever runs when there is at least one paired device to
    # run it for, and only while this node is actually reaching the network
    # at all -- with discovery forced offline there is nobody it could reach
    # anyway (see this function's own docstring).
    device_sync_active = (
        enable_device_sync and not _discovery_is_offline() and bool(devices_mod.load_devices())
    )

    background_tasks: list[asyncio.Task] = []
    if db_path is not None:
        background_tasks.append(asyncio.create_task(_refresh_index_if_needed(db_path)))

    if enable_lan_discovery:
        async def _on_lan_discovered(peer_pubkey_hex: str, peer_ticket: str) -> None:
            await _auto_sync_discovered_peer(
                peer_pubkey_hex, peer_ticket, identity=identity,
                peer_feeds_root=resolved_peer_feeds_root, peers_path=peers_path,
                db_path=db_path, relay=relay, source="lan",
                also_pull=list(acquire_state),
                known_tickets=known_device_tickets if device_sync_active else None,
                devices_dir=resolved_devices_dir if device_sync_active else None,
                device_sync_state_path=resolved_device_sync_state_path if device_sync_active else None,
            )

        background_tasks.append(asyncio.create_task(run_beacon(
            identity.public_key_hex, ticket, _on_lan_discovered,
            port=lan_discovery_port, interval_s=lan_discovery_interval_s,
        )))

    if enable_wan_discovery:
        async def _on_wan_discovered(peer_pubkey_hex: str, peer_ticket: str) -> None:
            await _auto_sync_discovered_peer(
                peer_pubkey_hex, peer_ticket, identity=identity,
                peer_feeds_root=resolved_peer_feeds_root, peers_path=peers_path,
                db_path=db_path, relay=relay, source="wan",
                also_pull=list(acquire_state),
                known_tickets=known_device_tickets if device_sync_active else None,
                devices_dir=resolved_devices_dir if device_sync_active else None,
                device_sync_state_path=resolved_device_sync_state_path if device_sync_active else None,
            )

        background_tasks.append(asyncio.create_task(run_wan_discovery(
            identity.public_key_hex, ticket, _on_wan_discovered,
            port=wan_discovery_port, lookup_interval_s=wan_discovery_interval_s,
            public_port=wan_public_port, auto_port=wan_auto_port, debug=debug,
        )))

    if publish_watch_dir is not None:
        background_tasks.append(asyncio.create_task(
            _watch_publish_loop(feed_dir, identity, publish_watch_dir, publish_watch_interval_s, db_path)
        ))

    background_tasks.append(asyncio.create_task(_prune_peers_loop(peers_path)))

    if replicate and replication_budget > 0 and db_path is not None:
        background_tasks.append(asyncio.create_task(_replication_loop(
            feed_dir, identity.public_key_hex, resolved_peer_feeds_root, peers_path,
            db_path, replication_budget, acquire_state,
        )))

    if device_sync_active:
        background_tasks.append(asyncio.create_task(_device_watch_loop(
            resolved_devices_dir, resolved_device_sync_state_path, identity, relay, known_device_tickets,
        )))

    try:
        while True:
            incoming = await ep.accept_next()
            if incoming is None:
                break
            asyncio.create_task(_handle_connection(
                incoming, feed_dir, peers_path, profile_path,
                resolved_peer_feeds_root, identity.public_key_hex,
                resolved_devices_dir, resolved_device_sync_state_path))
    finally:
        for task in background_tasks:
            task.cancel()
        await ep.close()


async def dial_with_fallback(ep: iroh.Endpoint, ticket_str: str, alpn: bytes, *,
                             timeout: float = CONNECT_TIMEOUT_S) -> iroh.Connection:
    """Dial a peer by its ticket, falling back to dialing by identity alone
    if the ticket's pinned addresses have gone stale -- the exact two-step
    dance sync_with_peer below always used, factored out so device_sync.py's
    device-pairing and device-sync dials (a different ALPN, same iroh
    endpoint conventions) can reuse it rather than re-deriving it.

    A ticket pins the addresses a peer had when it minted the ticket, and
    those go stale on its next restart or IP change -- but its identity
    never does. `preset_n0` (bind_endpoint) publishes every endpoint's
    current address to iroh's discovery service keyed by that identity, so
    a node id alone is enough to find it again. Verified end to end:
    dialling with relay_url=None and no direct addresses connected in ~2s
    and completed a real exchange. This is what makes a peer, once
    discovered, stay reachable even after its ticket has gone stale.
    """
    ticket = iroh.EndpointTicket.from_string(ticket_str)
    addr = ticket.endpoint_addr()
    try:
        return await asyncio.wait_for(ep.connect(addr, alpn), timeout)
    except (Exception, asyncio.TimeoutError):
        peer_id = addr.id()
        if not str(peer_id):
            raise
        try:
            return await asyncio.wait_for(ep.connect(iroh.EndpointAddr(peer_id, None, []), alpn), timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"could not reach peer {str(peer_id)[:16]}... within "
                f"{timeout:.0f}s -- it is probably offline, or neither side "
                "can open a connection to the other"
            ) from None


async def sync_with_peer(
    ticket_str: str,
    identity: Identity,
    peer_feeds_root: Path,
    peers_path: Path,
    *,
    relay: bool = True,
    added_via: str = "manual",
    limits: QuotaLimits | None = None,
    also_pull: list[str] | None = None,
) -> SyncReport:
    """Dial a peer, pull any new feed entries into a local mirror directory
    (subject to `limits` -- ARCHITECTURE.md's Abuse Resistance quotas,
    checked against cheap metadata before any content is fetched), and merge
    their known-peer list into ours (peer exchange gossip)."""
    limits = limits or QuotaLimits()
    ep = await bind_endpoint(identity, relay=relay)
    try:
        conn = await dial_with_fallback(ep, ticket_str, ALPN)
        peer_pubkey_hex = str(conn.remote_id())

        mirror_dir = Path(peer_feeds_root) / peer_pubkey_hex
        existing_entries = read_entries(mirror_dir)
        since_seq = len(existing_entries)

        meta_response = await _request(conn, {"op": "get_feed_meta", "since_seq": since_seq})
        if "error" in meta_response:
            raise RuntimeError(f"peer {peer_pubkey_hex} returned an error for get_feed_meta: {meta_response['error']}")
        candidate_entries = [FeedEntry(**d) for d in meta_response.get("entries", [])]
        quota_result = check_feed_metadata(existing_entries, candidate_entries, limits)

        new_entries: list[dict] = []
        if quota_result.allowed_count > 0:
            feed_response = await _request(
                conn, {"op": "get_feed", "since_seq": since_seq, "limit": quota_result.allowed_count},
            )
            if "error" in feed_response:
                raise RuntimeError(f"peer {peer_pubkey_hex} returned an error for get_feed: {feed_response['error']}")
            new_entries = feed_response.get("entries", [])
            for wire_entry in new_entries:
                entry, blob = _entry_from_wire(wire_entry)
                write_received_entry(mirror_dir, peer_pubkey_hex, entry, blob)

        verify_result = (
            verify_feed(mirror_dir, expected_pubkey_hex=peer_pubkey_hex)
            if mirror_dir.exists()
            else FeedVerifyResult(0, 0, None)
        )

        peers_response = await _request(conn, {"op": "get_peers"})
        if "error" in peers_response:
            raise RuntimeError(f"peer {peer_pubkey_hex} returned an error for get_peers: {peers_response['error']}")
        local_peers = load_peers(peers_path)
        for peer_dict in peers_response.get("peers", []):
            # peer_from_dict (not Peer(**...)) so a field a newer peer added
            # and this version doesn't know about yet is dropped instead of
            # raising TypeError and aborting the whole sync (see peers.py).
            gossiped = peer_from_dict({**peer_dict, "added_via": "gossip"})
            local_peers = upsert_peer(local_peers, gossiped)

        now = datetime.now(timezone.utc).isoformat()
        local_peers = upsert_peer(local_peers, Peer(
            ticket=ticket_str, feed_pubkey_hex=peer_pubkey_hex,
            first_seen=now, last_seen=now, added_via=added_via,
        ))
        # A peer's gossiped list is merged wholesale just above, and its
        # entries carry attacker-declared last_seen values, so this is where
        # an unbounded flood would otherwise be persisted. Cap it, evicting
        # gossip and the oldest first -- see peers.cap_peers.
        local_peers = cap_peers(local_peers)
        save_peers(local_peers, peers_path)

        # Ask for the peer's own signed profile (profile.py). Accepted only
        # if it verifies AND claims to be the same pubkey we just
        # authenticated over the QUIC handshake -- a peer may only ever
        # serve its own profile, never relay someone else's (the one thing
        # that would let a hostile node inject arbitrary like-graph edges).
        # An old peer that doesn't know this op yet answers
        # {"error": "unknown op 'get_profile'"} -- and any of that, or a
        # transport hiccup, or a malformed/unsigned/mismatched profile, is
        # treated as "this peer has no profile", never as a sync failure.
        peer_profile: dict | None = None
        try:
            profile_response = await _request(conn, {"op": "get_profile"})
            if "error" not in profile_response:
                candidate = profile_response.get("profile")
                if (
                    isinstance(candidate, dict)
                    and verify_profile(candidate)
                    and str(candidate.get("pubkey")) == peer_pubkey_hex
                ):
                    peer_profile = candidate
        except Exception:  # noqa: BLE001 -- a profile is a nice-to-have, never worth failing sync over
            peer_profile = None

        # Ask what feeds this peer holds (its own plus feeds it mirrors for
        # others). Backward compatible: a peer too old for the op answers
        # {"error": ...}, which is just "no digest", never a sync failure --
        # the same convention get_profile uses.
        held_feeds: list = []
        try:
            hf_response = await _request(conn, {"op": "get_held_feeds"})
            if "error" not in hf_response:
                held_feeds = [d for d in hf_response.get("feeds", [])
                              if isinstance(d, dict) and isinstance(d.get("pubkey"), str)]
        except Exception:  # noqa: BLE001 -- a digest is a nice-to-have, never worth failing sync over
            held_feeds = []

        # Pull the third-party feeds `also_pull` asked for that THIS peer
        # actually advertised holding. The advertised-set gate is load-bearing:
        # it is what stops us sending a pubkey'd get_feed to a peer too old to
        # understand the pubkey param (which would answer with its *own* feed
        # under the wrong identity). Every pulled feed is verified against its
        # claimed pubkey; a holder that serves garbage only wastes one round.
        pulled_feeds: list = []
        if also_pull:
            advertised = {d["pubkey"] for d in held_feeds}
            for target in also_pull:
                if target in (peer_pubkey_hex, identity.public_key_hex):
                    continue
                if target not in advertised:
                    continue
                try:
                    if await _pull_third_party_feed(conn, target, peer_feeds_root, limits):
                        pulled_feeds.append(target)
                except Exception:  # noqa: BLE001 -- one bad feed must not abort the sync
                    continue

        return SyncReport(
            peer_pubkey_hex=peer_pubkey_hex,
            new_entry_count=len(new_entries),
            verify=verify_result,
            quota=quota_result,
            peers_known=len(local_peers),
            profile=peer_profile,
            held_feeds=held_feeds,
            pulled_feeds=pulled_feeds,
        )
    finally:
        await ep.close()
