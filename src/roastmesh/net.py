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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import iroh

from roastmesh.feed import (
    FeedEntry,
    FeedVerifyResult,
    blob_path_for,
    default_peer_feeds_root,
    read_entries,
    verify_feed,
    write_received_entry,
)
from roastmesh.identity import Identity
from roastmesh.index import repository as repo
from roastmesh.index.db import connect
from roastmesh.index.ingest import ingest_feed
from roastmesh.lan_discovery import BEACON_INTERVAL_S, BEACON_PORT, run_beacon
from roastmesh.peers import Peer, load_peers, peer_from_dict, save_peers, upsert_peer
from roastmesh.profile import load_profile, verify_profile
from roastmesh.quota import QuotaCheckResult, QuotaLimits, check_feed_metadata
from roastmesh.wan_discovery import DHT_LOOKUP_INTERVAL_S, WAN_PORT, run_wan_discovery
from roastmesh.watch_folder import publish_new_files

ALPN = b"roastmesh/peer-sync/0"
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


def _build_response(request: dict, feed_dir: Path, peers_path: Path, profile_path: Path | None = None) -> dict:
    op = request.get("op")
    if op == "get_peers":
        return {"peers": [asdict(p) for p in load_peers(peers_path)]}
    if op == "get_feed_meta":
        since_seq = int(request.get("since_seq", 0))
        entries = [e for e in read_entries(feed_dir) if e.seq >= since_seq]
        return {"entries": [asdict(e) for e in entries]}
    if op == "get_feed":
        since_seq = int(request.get("since_seq", 0))
        entries = [e for e in read_entries(feed_dir) if e.seq >= since_seq]
        limit = request.get("limit")
        if limit is not None:
            entries = entries[:int(limit)]
        return {"entries": [_entry_to_wire(feed_dir, e) for e in entries]}
    if op == "get_profile":
        # A peer may only ever be served ITS OWN profile -- there is no
        # "profile of pubkey X" lookup here, deliberately: this node has no
        # way to know that a claimed third-party profile wasn't tampered
        # with in transit by whoever is answering. sync_with_peer enforces
        # the other half of that (profile["pubkey"] == conn.remote_id()).
        own_profile = load_profile(profile_path)
        return {"profile": own_profile.to_dict() if own_profile else None}
    return {"error": f"unknown op {op!r}"}


async def _handle_request(bi, feed_dir: Path, peers_path: Path, profile_path: Path | None = None) -> None:
    try:
        request = await _recv_message(bi)
        response = _build_response(request, feed_dir, peers_path, profile_path)
    except Exception as exc:
        response = {"error": str(exc)}
    try:
        await _send_message(bi, response)
    except Exception:
        pass  # peer likely disconnected mid-response; nothing more to do


async def _handle_connection(incoming, feed_dir: Path, peers_path: Path, profile_path: Path | None = None) -> None:
    try:
        accepting = await incoming.accept()
        conn = await accepting.connect()
    except Exception:
        return
    while True:
        try:
            bi = await conn.accept_bi()
        except Exception:
            return  # connection closed
        asyncio.create_task(_handle_request(bi, feed_dir, peers_path, profile_path))


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
) -> None:
    """Shared by LAN and internet (DHT) auto-discovery -- `source` (used
    both as a log-line prefix and as the peer's `added_via` tag) is the
    only thing that differs between the two callers."""
    print(f"{source}: discovered {peer_pubkey_hex[:16]}..., syncing", flush=True)
    try:
        report = await sync_with_peer(
            peer_ticket, identity, peer_feeds_root, peers_path, relay=relay, added_via=source,
        )
    except Exception as exc:  # noqa: BLE001 -- a bad/unreachable peer hint shouldn't kill serve()
        print(f"{source}: sync with {peer_pubkey_hex[:16]}... failed: {exc!r}", flush=True)
        return

    verify_msg = "OK" if report.verify.ok else f"INVALID: {report.verify.error}"
    print(f"{source}: synced with {peer_pubkey_hex[:16]}...: {report.new_entry_count} new entries, feed {verify_msg}",
          flush=True)

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
        finally:
            conn.close()


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

    ep = await bind_endpoint(identity, alpns=[ALPN], relay=relay)
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

    background_tasks: list[asyncio.Task] = []
    if db_path is not None:
        background_tasks.append(asyncio.create_task(_refresh_index_if_needed(db_path)))

    if enable_lan_discovery:
        async def _on_lan_discovered(peer_pubkey_hex: str, peer_ticket: str) -> None:
            await _auto_sync_discovered_peer(
                peer_pubkey_hex, peer_ticket, identity=identity,
                peer_feeds_root=resolved_peer_feeds_root, peers_path=peers_path,
                db_path=db_path, relay=relay, source="lan",
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
            )

        background_tasks.append(asyncio.create_task(run_wan_discovery(
            identity.public_key_hex, ticket, _on_wan_discovered,
            port=wan_discovery_port, lookup_interval_s=wan_discovery_interval_s,
            public_port=wan_public_port, auto_port=wan_auto_port,
        )))

    if publish_watch_dir is not None:
        background_tasks.append(asyncio.create_task(
            _watch_publish_loop(feed_dir, identity, publish_watch_dir, publish_watch_interval_s, db_path)
        ))

    try:
        while True:
            incoming = await ep.accept_next()
            if incoming is None:
                break
            asyncio.create_task(_handle_connection(incoming, feed_dir, peers_path, profile_path))
    finally:
        for task in background_tasks:
            task.cancel()
        await ep.close()


async def sync_with_peer(
    ticket_str: str,
    identity: Identity,
    peer_feeds_root: Path,
    peers_path: Path,
    *,
    relay: bool = True,
    added_via: str = "manual",
    limits: QuotaLimits | None = None,
) -> SyncReport:
    """Dial a peer, pull any new feed entries into a local mirror directory
    (subject to `limits` -- ARCHITECTURE.md's Abuse Resistance quotas,
    checked against cheap metadata before any content is fetched), and merge
    their known-peer list into ours (peer exchange gossip)."""
    limits = limits or QuotaLimits()
    ticket = iroh.EndpointTicket.from_string(ticket_str)
    addr = ticket.endpoint_addr()
    ep = await bind_endpoint(identity, relay=relay)
    try:
        try:
            conn = await ep.connect(addr, ALPN)
        except Exception:
            # A ticket pins the addresses a peer had when it minted the ticket,
            # and those go stale on its next restart or IP change -- but its
            # identity never does. `preset_n0` (bind_endpoint) publishes every
            # endpoint's current address to iroh's discovery service keyed by
            # that identity, so a node id alone is enough to find it again.
            # Verified end to end: dialling with relay_url=None and no direct
            # addresses connected in ~2s and completed a real feed exchange.
            # This is what makes a peer, once discovered, stay reachable.
            peer_id = addr.id()
            if not str(peer_id):
                raise
            conn = await ep.connect(iroh.EndpointAddr(peer_id, None, []), ALPN)
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

        return SyncReport(
            peer_pubkey_hex=peer_pubkey_hex,
            new_entry_count=len(new_entries),
            verify=verify_result,
            quota=quota_result,
            peers_known=len(local_peers),
            profile=peer_profile,
        )
    finally:
        await ep.close()
