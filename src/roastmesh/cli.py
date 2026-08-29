from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import click

import roastmesh
from roastmesh import net
from roastmesh.bootstrap import BOOTSTRAP_TICKETS
from roastmesh.feed import append_entry, default_feed_dir, default_peer_feeds_root, verify_feed
from roastmesh.gateway import make_server
from roastmesh.identity import load_or_create_identity
from roastmesh.index import repository as repo
from roastmesh.index.db import connect, get_meta, set_meta
from roastmesh.index.ingest import ingest_feed, ingest_file, ingest_path, refresh_known_sources
from roastmesh.machines import list_machines, slugify
from roastmesh.peers import Peer, default_peers_path, load_peers, node_id_from_ticket, prune_stale, save_peers, upsert_peer
from roastmesh.profile import load_or_default_profile, update_and_sign
from roastmesh.usernames import default_display_name
from roastmesh.watch_folder import default_watch_dir
from roastmesh import asyncio_policy

DEFAULT_DB = "roastmesh.sqlite3"


def _remind_backup_if_new(identity, created: bool) -> None:
    if created:
        click.echo(f"created new identity: {identity.public_key_hex}")
        click.echo(
            "run `roastmesh identity export` to back up your secret key -- "
            "it cannot be recovered if lost.", err=True,
        )


def _report_ingest_results(results) -> None:
    ingested = skipped = failed = 0
    for result in results:
        if result.error:
            failed += 1
            click.echo(f"error: {result.error}", err=True)
        elif result.skipped_duplicate:
            skipped += 1
        else:
            ingested += 1
    click.echo(f"ingested {ingested}, skipped (duplicate) {skipped}, failed {failed}")


@click.group()
@click.version_option(roastmesh.__version__, "-V", "--version", prog_name="roastmesh")
@click.option("--db", default=DEFAULT_DB, show_default=True, help="Path to the SQLite index.")
@click.pass_context
def main(ctx: click.Context, db: str) -> None:
    """Local parser, metadata extractor, and search index for Artisan .alog roast profiles."""
    # Before any asyncio.run() below -- see roastmesh.asyncio_policy for why
    # Windows needs a different loop, and why this is applied here rather than
    # from the package root.
    asyncio_policy.apply()
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--user-log", is_flag=True, help="Mark ingested file(s) as your own roasts.")
@click.pass_context
def ingest(ctx: click.Context, path: Path, user_log: bool) -> None:
    """Ingest a single .alog file, or every .alog file in a directory."""
    conn = connect(ctx.obj["db_path"])
    results = ingest_path(conn, path, is_user_log=user_log)
    _report_ingest_results(results)


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.pass_context
def reindex(ctx: click.Context, path: Path) -> None:
    """Wipe the index and rebuild it from every .alog file in PATH.

    The index is a pure function of the corpus, so this is always safe to
    run again -- it never needs to be kept in sync incrementally. Wiping
    does lose local-only annotations that live only in the index, not any
    .alog file, though: hidden status and "my own roasts" tagging (pass
    --user-log to `ingest`/`feed ingest` afterwards to restore the
    latter). `refresh` is the non-destructive alternative when that
    matters -- it re-ingests everything already known in place instead of
    starting over from a directory.
    """
    db_path = Path(ctx.obj["db_path"])
    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    try:
        results = ingest_path(conn, path)
    finally:
        # Closed explicitly, unlike the other commands here, because this is
        # the one that *deletes* the database. Windows takes mandatory file
        # locks, so a connection left open by an earlier reindex in the same
        # process makes the next one's unlink() fail with "the process cannot
        # access the file because it is being used by another process".
        # Elsewhere the handle is released at process exit, which is fine.
        conn.close()
    failed = sum(1 for r in results if r.error)
    click.echo(f"reindexed {len(results) - failed} roast(s) from {path}")
    for result in results:
        if result.error:
            click.echo(f"error: {result.error}", err=True)


@main.command()
@click.option("--force", is_flag=True, help="Refresh even if already up to date for this version.")
@click.pass_context
def refresh(ctx: click.Context, force: bool) -> None:
    """Re-ingest every already-known file to pick up parser/classification
    improvements for roasts indexed by an older version of roastmesh --
    without wiping anything (unlike `reindex`, this can't lose hidden
    status or "my own roasts" tagging, since it only touches rows that
    already exist).

    Cheap and safe to run unconditionally (it's what `node serve` does
    automatically on startup): skips instantly if this index was already
    refreshed for the currently-running version, unless --force.
    """
    conn = connect(ctx.obj["db_path"])
    current = roastmesh.__version__
    if not force and get_meta(conn, "refreshed_by_version") == current:
        click.echo(f"already up to date for v{current}")
        return
    results = refresh_known_sources(conn)
    refreshed = sum(1 for r in results if r.error is None)
    failed = sum(1 for r in results if r.error)
    click.echo(f"refreshed {refreshed} roast(s) for v{current}"
               + (f", {failed} error(s)" if failed else ""))
    for result in results:
        if result.error:
            click.echo(f"error: {result.error}", err=True)
    set_meta(conn, "refreshed_by_version", current)


def _filter_lan_only(rows: list, peers_file: Path) -> list:
    """Keep own roasts (source_type != "p2p") and roasts from peers whose
    `added_via` is "lan" (peer discovered by LAN broadcast, not the
    internet-wide DHT, a manual paste, or gossip); drop everything else.
    Provenance for a p2p row lives in peers.json, not the SQLite index --
    matched against `sources.author_pubkey` (row.author_pubkey), populated
    at ingest time from the same "<pubkey_hex>:<seq>" convention this used
    to split by hand (see ingest.ingest_file/ingest_feed).
    A peer no longer in peers.json (e.g. pruned since) is treated as
    unknown provenance and dropped too -- can't confirm "lan" for it."""
    added_via_by_pubkey = {p.feed_pubkey_hex: p.added_via for p in load_peers(peers_file)}
    kept = []
    for row in rows:
        if row.source_type != "p2p":
            kept.append(row)
            continue
        if added_via_by_pubkey.get(row.author_pubkey) == "lan":
            kept.append(row)
    return kept


@main.command()
@click.argument("text", required=False)
@click.option("--machine", "machine_key",
              help="Filter by machine_key, e.g. kaleido_m2. Also matches a roast whose own "
                   "machine is unknown but whose owner has declared that machine in their "
                   "profile (see `roastmesh profile set --machine`) -- this only ever widens "
                   "results, never narrows an existing --machine match.")
@click.option("--roast-type", help="Filter by roast_type, e.g. 'full city'.")
@click.option("--dtr-min", type=float, help="Minimum development time ratio (%).")
@click.option("--dtr-max", type=float, help="Maximum development time ratio (%).")
@click.option("--drop-after", "drop_bt_min", type=float, help="Minimum DROP bean temp (C).")
@click.option("--after-second-crack/--not-after-second-crack", "after_second_crack",
              default=None, help="Only roasts dropped at or after SC_START.")
@click.option("--lan-only/--all-peers", default=False, show_default=True,
              help="--lan-only restricts results to your own roasts and peers found on your "
                   "local network, hiding anything from the internet-wide DHT, a manually-added "
                   "peer, or gossip. The default is --all-peers: finding roasts from anywhere "
                   "is the point of the network, and a peer's entries are signature-verified "
                   "however they were discovered.")
@click.option("--peers-file", default=None, type=click.Path(path_type=Path),
              help="Peer list to check provenance against for --lan-only "
                   "(default: ~/.local/share/roastmesh/peers.json).")
@click.option("--own-only", is_flag=True,
              help="Only show your own roasts -- hide everything synced from any peer.")
@click.option("--show-hidden", is_flag=True,
              help="Also include roasts you've hidden (see `roastmesh hide`).")
@click.option("--user", "user_id", default=None,
              help="Only show roasts from one user (pubkey prefix, resolved like a roast id -- "
                   "see `roastmesh user show`).")
@click.option("--favorites-only", is_flag=True,
              help="Only show roasts from users you've favorited (see `roastmesh user favorite`).")
@click.option("--json", "as_json", is_flag=True, help="Output matches as a JSON array instead of text.")
@click.pass_context
def search(
    ctx: click.Context,
    text: str | None,
    machine_key: str | None,
    roast_type: str | None,
    dtr_min: float | None,
    dtr_max: float | None,
    drop_bt_min: float | None,
    after_second_crack: bool | None,
    lan_only: bool,
    peers_file: Path | None,
    own_only: bool,
    show_hidden: bool,
    user_id: str | None,
    favorites_only: bool,
    as_json: bool,
) -> None:
    """Search the local index. TEXT is matched against beans/notes/roast type."""
    conn = connect(ctx.obj["db_path"])
    user_pubkey = _resolve_user_id(conn, user_id) if user_id else None
    rows = repo.search_roasts(
        conn, text=text, machine_key=machine_key, roast_type=roast_type,
        dtr_min=dtr_min, dtr_max=dtr_max, drop_bt_min=drop_bt_min,
        after_second_crack=after_second_crack, own_only=own_only, include_hidden=show_hidden,
        user_pubkey=user_pubkey, favorites_only=favorites_only,
    )
    if own_only:
        lan_only = False  # own roasts are never peer-sourced -- nothing left for it to filter
    if lan_only:
        rows = _filter_lan_only(rows, peers_file or default_peers_path())
    if as_json:
        click.echo(json.dumps([asdict(row) for row in rows]))
        return
    if not rows:
        click.echo("no matches")
        return
    for row in rows:
        if row.title:
            title = row.title
        elif row.beans_text:
            title = row.beans_text.splitlines()[0][:50]
        else:
            title = "(untitled)"
        dtr = f"{row.dtr_pct:.1f}%" if row.dtr_pct is not None else "?"
        drop = f"{row.drop_bt_c:.0f}C" if row.drop_bt_c is not None else "?"
        hidden_note = " [hidden]" if row.hidden else ""
        click.echo(f"{row.roast_id[:8]}  {row.machine_key:<16} {row.roast_type or '?':<12} "
                   f"DTR={dtr:<7} DROP={drop:<6} {title}{hidden_note}")


def _resolve_roast_id(conn, roast_id_prefix: str) -> str:
    """ROAST_ID arguments across show/hide/unhide may be a prefix, e.g. the
    8 characters `search` displays -- resolve it to exactly one full id,
    or fail clearly if it matches none or more than one."""
    matches = repo.find_ids_by_prefix(conn, roast_id_prefix)
    if not matches:
        raise click.ClickException(f"no roast found matching {roast_id_prefix!r}")
    if len(matches) > 1:
        raise click.ClickException(f"{roast_id_prefix!r} matches {len(matches)} roasts -- use more characters")
    return matches[0]


def _resolve_user_id(conn, pubkey_prefix: str) -> str:
    """Same shape as _resolve_roast_id, for a user's pubkey prefix -- used
    by `search --user`, `user show`, `user favorite`/`unfavorite`, and
    `user like`/`unlike`."""
    matches = repo.find_user_pubkeys_by_prefix(conn, pubkey_prefix)
    if not matches:
        raise click.ClickException(f"no user found matching {pubkey_prefix!r}")
    if len(matches) > 1:
        raise click.ClickException(f"{pubkey_prefix!r} matches {len(matches)} users -- use more characters")
    return matches[0]


@main.command("show")
@click.argument("roast_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON instead of text.")
@click.pass_context
def show(ctx: click.Context, roast_id: str, as_json: bool) -> None:
    """Show one roast's full detail (ROAST_ID may be a prefix, e.g. the 8
    characters `search` displays) and the on-disk path to its original
    .alog file, for opening it in Artisan directly."""
    conn = connect(ctx.obj["db_path"])
    full_id = _resolve_roast_id(conn, roast_id)
    record = repo.load_full_record(conn, full_id)
    raw_path = repo.find_raw_path(conn, full_id)
    hidden = repo.find_hidden(conn, full_id)

    if as_json:
        click.echo(json.dumps({"record": record, "raw_path": raw_path, "hidden": hidden}))
        return

    beans = record.get("beans_text") or "(no beans text)"
    click.echo(beans.splitlines()[0])
    click.echo(f"machine: {record.get('machine_key')} ({record.get('roaster_type_raw')})")
    roast_type_note = " (estimated from peak temperature -- may not hold for every machine's probe)" \
        if record.get("roast_type") else ""
    click.echo(f"roast type: {record.get('roast_type') or '?'}{roast_type_note}")
    click.echo(f"batch weight in/out: {record.get('batch_weight_in_g')}g / {record.get('batch_weight_out_g')}g")
    click.echo(f"roast date: {record.get('roast_date') or '?'}")
    for m in record.get("milestones") or []:
        click.echo(f"  {m.get('name'):<10} t={m.get('time_s')}  BT={m.get('bt_c')}  ET={m.get('et_c')}")
    if record.get("roasting_notes"):
        click.echo(f"notes: {record['roasting_notes']}")
    click.echo(f"file: {raw_path}")
    if hidden:
        click.echo("hidden: yes -- hidden from your own search results (unhide to see it there again)")


@main.command("hide")
@click.argument("roast_id")
@click.pass_context
def hide(ctx: click.Context, roast_id: str) -> None:
    """Hide one roast from your own search results.

    Local only: doesn't touch the feed, so it doesn't retroactively remove
    anything already replicated to a peer, and doesn't stop it being
    replicated to a peer syncing for the first time in the future either --
    a signed, hash-chained feed entry can't be selectively unpublished
    without breaking the chain for every entry after it. `search --show-hidden`
    still finds it if you want to `unhide` it later.
    """
    conn = connect(ctx.obj["db_path"])
    full_id = _resolve_roast_id(conn, roast_id)
    repo.set_hidden(conn, full_id, True)
    click.echo(f"hidden {full_id[:8]}... (local only -- see `roastmesh hide --help`)")


@main.command("unhide")
@click.argument("roast_id")
@click.pass_context
def unhide(ctx: click.Context, roast_id: str) -> None:
    """Un-hide a previously hidden roast."""
    conn = connect(ctx.obj["db_path"])
    full_id = _resolve_roast_id(conn, roast_id)
    repo.set_hidden(conn, full_id, False)
    click.echo(f"unhidden {full_id[:8]}...")


@main.group()
def identity() -> None:
    """Manage your Ed25519 feed identity (created silently on first publish)."""


@identity.command("show")
def identity_show() -> None:
    """Print your public key, creating an identity first if none exists yet."""
    ident, created = load_or_create_identity()
    _remind_backup_if_new(ident, created)
    click.echo(ident.public_key_hex)


@identity.command("export")
def identity_export() -> None:
    """Print your secret key as hex, once, so you can back it up.

    There is no recovery if this is lost -- see ARCHITECTURE.md's Key
    Handling section. Treat this output like a password.
    """
    ident, _ = load_or_create_identity()
    click.echo(ident.secret_key_hex)


@main.group()
@click.option("--feed-dir", default=None, type=click.Path(path_type=Path),
              help="Feed directory (default: ~/.local/share/roastmesh/feed).")
@click.pass_context
def feed(ctx: click.Context, feed_dir: Path | None) -> None:
    """Manage your local append-only signed feed."""
    ctx.ensure_object(dict)
    ctx.obj["feed_dir"] = feed_dir or default_feed_dir()


@feed.command("publish")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def feed_publish(ctx: click.Context, path: Path) -> None:
    """Append a .alog file to your feed, signed with your identity, and add
    it to your own local search index (as one of "your own roasts")."""
    ident, created = load_or_create_identity()
    _remind_backup_if_new(ident, created)
    entry = append_entry(ctx.obj["feed_dir"], ident, path, timestamp=datetime.now(timezone.utc).isoformat())
    click.echo(f"published entry {entry.seq} ({entry.content_sha256[:12]}...) "
               f"to feed {ident.public_key_hex[:12]}...")
    conn = connect(ctx.obj["db_path"])
    result = ingest_file(conn, path, is_user_log=True)
    if result.error:
        click.echo(f"warning: could not add it to your local search index: {result.error}", err=True)


@feed.command("verify")
@click.option("--pubkey", "pubkey_hex", default=None,
              help="Expected public key (hex). Defaults to the feed's own pubkey.txt.")
@click.pass_context
def feed_verify(ctx: click.Context, pubkey_hex: str | None) -> None:
    """Check a feed's signature chain and blob integrity."""
    result = verify_feed(ctx.obj["feed_dir"], pubkey_hex)
    if result.ok:
        click.echo(f"OK: {result.valid_count} valid entries")
        return
    click.echo(f"INVALID after {result.valid_count}/{result.total_count} entries: {result.error}", err=True)
    raise SystemExit(1)


@feed.command("ingest")
@click.option("--pubkey", "pubkey_hex", required=True, help="Expected public key (hex) for this feed.")
@click.option("--user-log", is_flag=True,
              help="Mark these as your own roasts -- use when --feed-dir/--pubkey is your own "
                   "feed, not a peer's, so they're searchable/filterable as \"my own roasts\".")
@click.pass_context
def feed_ingest(ctx: click.Context, pubkey_hex: str, user_log: bool) -> None:
    """Verify a feed, then load its valid entries into the search index."""
    conn = connect(ctx.obj["db_path"])
    results = ingest_feed(
        conn, ctx.obj["feed_dir"], expected_pubkey_hex=pubkey_hex,
        source_type="local" if user_log else "p2p", is_user_log=user_log,
    )
    _report_ingest_results(results)


@main.group()
def node() -> None:
    """Run a long-lived roastmesh node."""


@node.command("serve")
@click.option("--feed-dir", default=None, type=click.Path(path_type=Path),
              help="Feed directory to serve (default: ~/.local/share/roastmesh/feed).")
@click.option("--peers-file", default=None, type=click.Path(path_type=Path),
              help="Peer list to serve (default: ~/.local/share/roastmesh/peers.json).")
@click.option("--no-relay", is_flag=True,
              help="Disable Iroh's relay/hole-punch (same-machine/LAN testing only).")
@click.option("--no-lan-discovery", is_flag=True,
              help="Don't broadcast/listen for other roastmesh nodes on the local network.")
@click.option("--wan-discovery", is_flag=True,
              help="Find other roastmesh nodes over the whole internet, via the public "
                   "BitTorrent DHT (opt-in: unlike LAN discovery, this makes your public "
                   "IP address visible to anyone else looking at the same DHT swarm).")
@click.option("--wan-port", default=None, type=int,
              help="UDP port for internet-wide discovery (default: 41890). Only needs "
                   "changing to run two nodes on one machine, or if something else "
                   "already holds that port.")
@click.option("--publish-watch-dir", default=None, type=click.Path(path_type=Path),
              help="Folder to auto-publish any .alog files dropped into it "
                   "(default: ~/RoastMeshShare).")
@click.option("--no-publish-watch", is_flag=True,
              help="Don't auto-publish files from the watch folder.")
@click.pass_context
def node_serve(
    ctx: click.Context, feed_dir: Path | None, peers_file: Path | None,
    no_relay: bool, no_lan_discovery: bool, wan_discovery: bool,
    wan_port: int | None, publish_watch_dir: Path | None, no_publish_watch: bool,
) -> None:
    """Listen for peer connections and answer get_peers/get_feed requests.

    Your Iroh node identity IS your feed's Ed25519 identity -- the ticket
    printed here dials the exact key your feed entries are signed with.

    Unless --no-lan-discovery, also finds other roastmesh nodes on the same
    local network automatically (no ticket-pasting needed) and syncs with
    them, ingesting into --db. With --wan-discovery, does the same over the
    whole internet via the public BitTorrent DHT.

    Unless --no-publish-watch, also auto-publishes any .alog file dropped
    into --publish-watch-dir (default ~/RoastMeshShare) -- no `feed publish`
    needed for files placed there.
    """
    ident, created = load_or_create_identity()
    _remind_backup_if_new(ident, created)
    feed_dir = feed_dir or default_feed_dir()
    peers_file = peers_file or default_peers_path()
    watch_dir = None if no_publish_watch else (publish_watch_dir or default_watch_dir())
    asyncio.run(net.serve(
        ident, feed_dir, peers_file, relay=not no_relay,
        db_path=ctx.obj["db_path"], enable_lan_discovery=not no_lan_discovery,
        enable_wan_discovery=wan_discovery, publish_watch_dir=watch_dir,
        **({"wan_discovery_port": wan_port} if wan_port else {}),
    ))


@node.command("doctor")
@click.option("--announce/--no-announce", default=False, show_default=True,
              help="Also publish this node to the DHT swarm while diagnosing.")
def node_doctor(announce: bool) -> None:
    """Diagnose internet-wide (DHT) peer discovery, step by step.

    Internet discovery is the one part of roastmesh that can fail completely
    while looking like it is running: the lookup happens in a background task,
    a failed round leaves no trace, and "no peers" is indistinguishable from
    "announced into the void". This prints what actually happened -- which
    bootstrap routers answered, how close to the swarm the lookup converged,
    how many nodes accepted the announcement -- so a report of "it doesn't
    work" comes with evidence instead of a shrug.
    """
    import hashlib

    from roastmesh.dht import DhtClient, LookupStats, load_node_cache, save_node_cache
    from roastmesh.wan_discovery import (
        DEFAULT_DHT_BOOTSTRAP,
        SWARM_INFO_HASH,
        _resolve,
        default_node_cache_path,
    )

    async def run() -> None:
        ident, _created = load_or_create_identity()
        click.echo(f"this node: {ident.public_key_hex}")

        cache_path = default_node_cache_path()
        cache = load_node_cache(cache_path)
        click.echo(f"node cache: {len(cache)} known-live DHT node(s) at {cache_path}")

        click.echo("\nbootstrap routers:")
        resolved = await _resolve(DEFAULT_DHT_BOOTSTRAP)
        resolved_set = set(resolved)
        client = await DhtClient.bind(port=0, own_id=hashlib.sha1(bytes.fromhex(ident.public_key_hex)).digest())
        try:
            reachable = 0
            for (host, port), addr in zip(DEFAULT_DHT_BOOTSTRAP, resolved):
                if addr not in resolved_set:
                    continue
                reply = await client.ping(addr, timeout=4.0)
                click.echo(f"  {host:26} {addr[0]:>15}  {'ok' if reply else 'no reply'}")
                reachable += 1 if reply else 0
            unresolved = len(DEFAULT_DHT_BOOTSTRAP) - len(resolved)
            if unresolved:
                click.echo(f"  ({unresolved} did not resolve)")
            if not reachable and not cache:
                click.echo("\nno bootstrap router answered and no cached nodes -- "
                           "the DHT is unreachable from this network.")
                return

            click.echo("\nswarm lookup:")
            seeds = list(dict.fromkeys([*resolved, *cache]))
            stats = LookupStats()
            peers = await client.discover_and_announce_peers(
                SWARM_INFO_HASH, seeds, seed_ids=dict(cache), announce=announce, stats=stats,
            )
            cache.update(dict(stats.live_nodes))
            save_node_cache(cache_path, cache)

            click.echo(f"  {stats.summary()}")
            if stats.closest_bits is not None and stats.closest_bits > 140:
                click.echo("  WARNING: the lookup never got near the swarm -- it is not converging.")
            if announce and stats.announced == 0:
                click.echo("  WARNING: no node accepted the announcement, so nobody can find this node.")
            if peers:
                click.echo(f"\n  {len(peers)} address(es) advertised on the roastmesh swarm:")
                for addr in sorted(peers):
                    click.echo(f"    {addr[0]}:{addr[1]}")
                click.echo("  (some may be unrelated DHT spam; each still has to pass the "
                           "roastmesh handshake before it counts as a peer)")
            else:
                click.echo("\n  no roastmesh peers currently advertised. If another node is "
                           "serving with --wan-discovery right now, re-run in a minute -- "
                           "announcements take a round to propagate.")
        finally:
            client.close()

    asyncio.run(run())


@main.group()
@click.option("--peers-file", default=None, type=click.Path(path_type=Path),
              help="Peer list (default: ~/.local/share/roastmesh/peers.json).")
@click.option("--peer-feeds-root", default=None, type=click.Path(path_type=Path),
              help="Where replicated peer feeds are mirrored (default: ~/.local/share/roastmesh/peer_feeds).")
@click.pass_context
def peer(ctx: click.Context, peers_file: Path | None, peer_feeds_root: Path | None) -> None:
    """Manage known peers and sync feeds with them."""
    ctx.ensure_object(dict)
    ctx.obj["peers_file"] = peers_file or default_peers_path()
    ctx.obj["peer_feeds_root"] = peer_feeds_root or default_peer_feeds_root()


@peer.command("add")
@click.argument("ticket")
@click.pass_context
def peer_add(ctx: click.Context, ticket: str) -> None:
    """Manually add a peer by pasting their ticket (paste-a-key from a friend)."""
    pubkey = node_id_from_ticket(ticket)
    if pubkey is None:
        raise click.ClickException(f"not a valid ticket: {ticket!r}")
    now = datetime.now(timezone.utc).isoformat()
    peers = load_peers(ctx.obj["peers_file"])
    peers = upsert_peer(peers, Peer(
        ticket=ticket, feed_pubkey_hex=pubkey, first_seen=now, last_seen=now, added_via="manual",
    ))
    save_peers(peers, ctx.obj["peers_file"])
    click.echo(f"added peer {pubkey[:16]}...")


@peer.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output peers as a JSON array instead of text.")
@click.pass_context
def peer_list(ctx: click.Context, as_json: bool) -> None:
    """List known peers."""
    peers = load_peers(ctx.obj["peers_file"])
    if as_json:
        click.echo(json.dumps([asdict(p) for p in peers]))
        return
    if not peers:
        click.echo("no known peers")
        return
    for p in peers:
        pubkey = (p.feed_pubkey_hex or "?")[:16]
        click.echo(f"{pubkey}...  last_seen={p.last_seen}  via={p.added_via}")


def _sync_and_ingest(ctx: click.Context, ticket: str, added_via: str) -> None:
    ident, created = load_or_create_identity()
    _remind_backup_if_new(ident, created)
    report = asyncio.run(net.sync_with_peer(
        ticket, ident, ctx.obj["peer_feeds_root"], ctx.obj["peers_file"], added_via=added_via,
    ))
    verify_msg = "OK" if report.verify.ok else f"INVALID: {report.verify.error}"
    click.echo(f"synced with {report.peer_pubkey_hex[:16]}...: {report.new_entry_count} new entries, "
               f"feed {verify_msg}, {report.peers_known} peers known")
    if report.quota.held_back:
        click.echo(f"{report.quota.held_back} entries held back by quota: {report.quota.reason}")

    mirror_dir = Path(ctx.obj["peer_feeds_root"]) / report.peer_pubkey_hex
    conn = connect(ctx.obj["db_path"])
    results = ingest_feed(conn, mirror_dir, expected_pubkey_hex=report.peer_pubkey_hex)
    _report_ingest_results(results)
    if report.profile is not None:
        # Persisted regardless of whether ingest_feed found anything new --
        # a peer who has already published everything they ever will still
        # deserves a name (see net.py's _auto_sync_discovered_peer, which
        # follows the same rule for LAN/WAN auto-sync).
        net.persist_peer_profile(conn, report.profile)


@peer.command("sync")
@click.argument("ticket")
@click.pass_context
def peer_sync(ctx: click.Context, ticket: str) -> None:
    """Pull a peer's new feed entries and merge their peer list, then index what was pulled."""
    _sync_and_ingest(ctx, ticket, added_via="manual")


@peer.command("prune")
@click.option("--max-age-days", type=float, default=30.0, show_default=True)
@click.pass_context
def peer_prune(ctx: click.Context, max_age_days: float) -> None:
    """Drop peers not seen within --max-age-days. Their replicated data stays in the index."""
    peers = load_peers(ctx.obj["peers_file"])
    kept = prune_stale(peers, max_age_days=max_age_days)
    save_peers(kept, ctx.obj["peers_file"])
    click.echo(f"pruned {len(peers) - len(kept)} peer(s), {len(kept)} remaining")


@peer.command("bootstrap")
@click.pass_context
def peer_bootstrap(ctx: click.Context) -> None:
    """Sync with every configured bootstrap peer."""
    if not BOOTSTRAP_TICKETS:
        click.echo("no bootstrap nodes configured yet -- use `roastmesh peer add <ticket>` "
                   "with a ticket from a friend, or ask the maintainers for one.")
        return
    for ticket in BOOTSTRAP_TICKETS:
        _sync_and_ingest(ctx, ticket, added_via="bootstrap")


@main.group()
def profile() -> None:
    """Manage your own signed profile: display name, declared machine, likes."""


@profile.command("show")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON instead of text.")
def profile_show(as_json: bool) -> None:
    """Show your own profile -- a fresh (unsaved) default one, seeded from
    your identity, if you've never set anything yet."""
    ident, created = load_or_create_identity()
    _remind_backup_if_new(ident, created)
    prof = load_or_default_profile(ident)
    if as_json:
        click.echo(json.dumps(prof.to_dict()))
        return
    click.echo(f"name: {prof.name}")
    click.echo(f"pubkey: {prof.pubkey}")
    click.echo(f"machine: {prof.machine_display or prof.machine_key or '(not set)'}")
    click.echo(f"likes: {len(prof.likes)}")


@profile.command("set")
@click.option("--name", "name", default=None,
              help="Display name shown to peers -- cosmetic only, never trusted for uniqueness.")
@click.option("--machine", "machine_key", default=None,
              help="Machine catalogue key, e.g. aillio_bullet_r1 (see `roastmesh machines list`).")
@click.option("--machine-custom", "machine_custom", default=None,
              help="Free-text machine name if yours isn't in the catalogue (e.g. a home-built rig).")
@click.pass_context
def profile_set(ctx: click.Context, name: str | None, machine_key: str | None,
                machine_custom: str | None) -> None:
    """Update your own display name and/or declared machine, then re-sign
    profile.json. Any field left unset keeps its previous value."""
    if machine_key and machine_custom:
        raise click.ClickException("--machine and --machine-custom are mutually exclusive")

    ident, created = load_or_create_identity()
    _remind_backup_if_new(ident, created)

    kwargs: dict = {}
    if name is not None:
        kwargs["name"] = name
    if machine_custom is not None:
        kwargs["machine_key"] = slugify(machine_custom)
        kwargs["machine_display"] = machine_custom
    elif machine_key is not None:
        match = next((m for m in list_machines() if m.key == machine_key), None)
        if match is None:
            raise click.ClickException(
                f"unknown machine key {machine_key!r} -- see `roastmesh machines list`, "
                "or use --machine-custom for a machine not in the catalogue"
            )
        kwargs["machine_key"] = match.key
        kwargs["machine_display"] = match.display_name

    updated = update_and_sign(ident, **kwargs)

    # Mirror your own profile into the index's `users` table. Without this the
    # owner-machine fallback in `search --machine` is dead on arrival for your
    # own roasts: that filter is a LEFT JOIN from sources.author_pubkey onto
    # users, so a machine you declared but that the index has never heard of
    # matches nothing. profile.json alone is not enough -- it is not a table
    # SQL can join against. Caught end to end (declare a machine, search for
    # it, get "no matches"), not by either phase's unit tests, which each
    # exercised one side of the join.
    conn = connect(ctx.obj["db_path"])
    try:
        repo.upsert_user_from_profile(
            conn,
            pubkey_hex=updated.pubkey,
            display_name=updated.name,
            machine_key=updated.machine_key,
            machine_display=updated.machine_display,
            profile_updated_at=updated.updated_at,
        )
        repo.claim_orphan_local_sources(conn, updated.pubkey)
    finally:
        conn.close()

    machine_note = f" ({updated.machine_display})" if updated.machine_display else ""
    click.echo(f"profile updated: {updated.name}{machine_note}")


@main.group()
def user() -> None:
    """Browse the users behind the pubkeys in your index: names, machines,
    favorites, and likes."""


@user.command("list")
@click.option("--machine", "machine_key", default=None, help="Only users who declared this machine_key.")
@click.option("--favorites", is_flag=True, help="Only users you've favorited.")
@click.option("--with-roasts/--all", "with_roasts", default=True, show_default=True,
              help="--with-roasts (the default) shows only users who have actually published a "
                   "roast into your index. --all also lists every other known peer, even one "
                   "that has never published anything.")
@click.option("--json", "as_json", is_flag=True, help="Output users as a JSON array instead of text.")
@click.pass_context
def user_list(ctx: click.Context, machine_key: str | None, favorites: bool, with_roasts: bool, as_json: bool) -> None:
    """List known users."""
    conn = connect(ctx.obj["db_path"])
    rows = repo.list_users(conn, machine_key=machine_key, favorites_only=favorites, with_roasts_only=with_roasts)
    for row in rows:
        if not row.display_name:
            # Every user renders with a name -- even one whose profile has
            # never synced -- via the same deterministic fallback the GUI
            # and profile.py itself use (see usernames.py).
            row.display_name = default_display_name(row.pubkey_hex)
    if as_json:
        click.echo(json.dumps([asdict(row) for row in rows]))
        return
    if not rows:
        click.echo("no users")
        return
    for row in rows:
        machine = row.machine_display or row.machine_key or "?"
        fav = " *favorite" if row.is_favorite else ""
        click.echo(f"{row.pubkey_hex[:8]}  {row.display_name:<20} {machine:<24} "
                   f"roasts={row.roast_count} likes={row.like_count}{fav}")


@user.command("show")
@click.argument("id_prefix")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON instead of text.")
@click.pass_context
def user_show(ctx: click.Context, id_prefix: str, as_json: bool) -> None:
    """Show one user's detail. ID_PREFIX may be a prefix of their pubkey."""
    conn = connect(ctx.obj["db_path"])
    pubkey = _resolve_user_id(conn, id_prefix)
    row = next((r for r in repo.list_users(conn, with_roasts_only=False) if r.pubkey_hex == pubkey), None)
    if row is None:
        raise click.ClickException(f"no user found matching {id_prefix!r}")
    if not row.display_name:
        row.display_name = default_display_name(row.pubkey_hex)
    if as_json:
        click.echo(json.dumps(asdict(row)))
        return
    click.echo(f"name: {row.display_name}")
    click.echo(f"pubkey: {row.pubkey_hex}")
    click.echo(f"machine: {row.machine_display or row.machine_key or '(unknown)'}")
    click.echo(f"roasts: {row.roast_count}")
    click.echo(f"likes: {row.like_count}")
    click.echo(f"favorite: {'yes' if row.is_favorite else 'no'}")
    click.echo(f"last seen: {row.last_seen or '?'}")


@user.command("favorite")
@click.argument("id_prefix")
@click.pass_context
def user_favorite(ctx: click.Context, id_prefix: str) -> None:
    """Favorite a user -- local only, never seen by peers (see schema.sql's
    users.is_favorite)."""
    conn = connect(ctx.obj["db_path"])
    pubkey = _resolve_user_id(conn, id_prefix)
    repo.ensure_user(conn, pubkey)
    repo.set_user_favorite(conn, pubkey, True)
    click.echo(f"favorited {pubkey[:8]}...")


@user.command("unfavorite")
@click.argument("id_prefix")
@click.pass_context
def user_unfavorite(ctx: click.Context, id_prefix: str) -> None:
    """Un-favorite a previously favorited user."""
    conn = connect(ctx.obj["db_path"])
    pubkey = _resolve_user_id(conn, id_prefix)
    repo.set_user_favorite(conn, pubkey, False)
    click.echo(f"unfavorited {pubkey[:8]}...")


@user.command("like")
@click.argument("id_prefix")
@click.pass_context
def user_like(ctx: click.Context, id_prefix: str) -> None:
    """Like a user -- public and attributable: recorded in your own signed
    profile.json, so any peer who syncs with you can see who you liked."""
    conn = connect(ctx.obj["db_path"])
    pubkey = _resolve_user_id(conn, id_prefix)
    ident, created = load_or_create_identity()
    _remind_backup_if_new(ident, created)
    current = load_or_default_profile(ident)
    likes = list(current.likes)
    if pubkey not in likes:
        likes.append(pubkey)
    update_and_sign(ident, likes=likes)
    repo.ensure_user(conn, pubkey)
    repo.add_user_like(conn, ident.public_key_hex, pubkey)
    click.echo(f"liked {pubkey[:8]}...")


@user.command("unlike")
@click.argument("id_prefix")
@click.pass_context
def user_unlike(ctx: click.Context, id_prefix: str) -> None:
    """Undo a previous like."""
    conn = connect(ctx.obj["db_path"])
    pubkey = _resolve_user_id(conn, id_prefix)
    ident, created = load_or_create_identity()
    _remind_backup_if_new(ident, created)
    current = load_or_default_profile(ident)
    likes = [p for p in current.likes if p != pubkey]
    update_and_sign(ident, likes=likes)
    repo.remove_user_like(conn, ident.public_key_hex, pubkey)
    click.echo(f"unliked {pubkey[:8]}...")


@main.group()
def machines() -> None:
    """The roaster machine catalogue -- a search facet and the profile's machine picker."""


@machines.command("list")
@click.option("--used", is_flag=True,
              help="List only the machine_keys actually present in your index (for an "
                   "autocomplete), instead of the full catalogue.")
@click.option("--json", "as_json", is_flag=True, help="Output as a JSON array instead of text.")
@click.pass_context
def machines_list(ctx: click.Context, used: bool, as_json: bool) -> None:
    """List the machine catalogue, or (--used) the machine_keys already in your index."""
    if used:
        conn = connect(ctx.obj["db_path"])
        keys = repo.find_distinct_machine_keys(conn)
        if as_json:
            click.echo(json.dumps(keys))
            return
        for key in keys:
            click.echo(key)
        return

    catalogue = list_machines()
    if as_json:
        click.echo(json.dumps([asdict(m) for m in catalogue]))
        return
    for m in catalogue:
        click.echo(f"{m.key:<28} {m.display_name} ({m.manufacturer})")


@main.group()
def gateway() -> None:
    """Serve your local index as a read-only web view."""


@gateway.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind address. Use 0.0.0.0 to expose beyond localhost.")
@click.option("--port", default=8420, show_default=True, type=int)
@click.pass_context
def gateway_serve(ctx: click.Context, host: str, port: int) -> None:
    """Start the read-only web gateway: search, browse, download -- GET-only, never writes."""
    server = make_server(ctx.obj["db_path"], host=host, port=port)
    print(f"serving on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
