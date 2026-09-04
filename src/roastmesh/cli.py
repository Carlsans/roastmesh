from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import click

import roastmesh
from roastmesh import net
from roastmesh import replication
from roastmesh.bootstrap import BOOTSTRAP_TICKETS
from roastmesh.feed import (append_entry, default_feed_dir, default_peer_feeds_root,
                            held_feeds_digest, verify_feed)
from roastmesh.gateway import make_server
from roastmesh.identity import load_or_create_identity
from roastmesh.index import repository as repo
from roastmesh.index.db import connect, get_meta, set_meta
from roastmesh.index.ingest import ingest_feed, ingest_file, ingest_path, refresh_known_sources
from roastmesh.machines import list_machines, slugify
from roastmesh.peers import (Peer, default_peers_path, load_peers, node_id_from_ticket,
                             prune_stale, public_ip_from_ticket, save_peers, upsert_peer)
from roastmesh.profile import load_or_default_profile, update_and_sign
from roastmesh.usernames import default_display_name
from roastmesh.watch_folder import default_watch_dir
from roastmesh import asyncio_policy

DEFAULT_DB = "roastmesh.sqlite3"


def _remind_backup_if_new(identity, created: bool) -> None:
    if created:
        # stderr, like the line below it: this is a notice, not output. On
        # stdout it corrupted every `--json` command that happened to be the
        # one creating the identity -- the payload became "created new
        # identity: ...\n{...}", which is not JSON.
        click.echo(f"created new identity: {identity.public_key_hex}", err=True)
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
    blob_local = repo.is_blob_local(conn, full_id)
    feed_pubkey = repo.feed_pubkey_for_roast(conn, full_id)
    conn.close()

    # A stub -- the metadata is indexed but the .alog bytes were evicted to
    # reclaim disk. Fetch them on demand from a holder so the file path below
    # actually resolves.
    fetched = False
    if blob_local is False and feed_pubkey:
        fetched = _fetch_stub_on_demand(ctx, feed_pubkey)
        blob_local = fetched or blob_local

    if as_json:
        click.echo(json.dumps({"record": record, "raw_path": raw_path,
                               "hidden": hidden, "blob_local": bool(blob_local)}))
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
    if blob_local is False:
        conn2 = connect(ctx.obj["db_path"])
        n = len(repo.known_holders(conn2, feed_pubkey)) if feed_pubkey else 0
        conn2.close()
        click.echo(f"file: not downloaded -- held by {n} peer(s), none reachable right now")
    else:
        if fetched:
            click.echo("(fetched on demand from a peer)")
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
@click.option("--public-port", default=None, metavar="PORT|auto",
              help="The port other machines can reach this one on, when your router or "
                   "VPN forwards one to you. Set it and internet discovery publishes that "
                   "port instead of the one your NAT happened to use, which is the only "
                   "thing that works behind a port forward. Use `auto` to ask the router "
                   "for one (PCP/NAT-PMP) and renew it automatically. Run `node doctor` -- "
                   "it says outright when you need this.")
@click.option("--publish-watch-dir", default=None, type=click.Path(path_type=Path),
              help="Folder to auto-publish any .alog files dropped into it "
                   "(default: ~/RoastMeshShare).")
@click.option("--no-publish-watch", is_flag=True,
              help="Don't auto-publish files from the watch folder.")
@click.option("--debug", "debug_logging", is_flag=True,
              help="Verbose network logging for diagnostics -- extra DHT/sync detail. "
                   "The GUI's Network tab can turn this on and save the log to send for support.")
@click.option("--no-replicate", is_flag=True,
              help="Don't mirror other users' feeds for resilience. By default this node "
                   "keeps a bounded cache of feeds it learns about (even ones it never "
                   "synced directly) so a roast stays available while its author is "
                   "offline. Note: this is unrelated to --no-relay, which is Iroh's "
                   "hole-punch relay.")
@click.option("--replication-budget", default=None, metavar="SIZE",
              help="Max disk for mirrored peer feeds, e.g. 500MB or 2GB (default: 500MB). "
                   "When full, the rarest feeds are kept and the most-replicated evicted "
                   "to search-only stubs, fetched again on demand. 0 disables replication.")
@click.pass_context
def node_serve(
    ctx: click.Context, feed_dir: Path | None, peers_file: Path | None,
    no_relay: bool, no_lan_discovery: bool, wan_discovery: bool,
    wan_port: int | None, public_port: str | None,
    publish_watch_dir: Path | None, no_publish_watch: bool,
    no_replicate: bool, replication_budget: str | None, debug_logging: bool,
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
    auto_port, fixed_port = _parse_public_port(public_port)
    ident, created = load_or_create_identity()
    _remind_backup_if_new(ident, created)
    feed_dir = feed_dir or default_feed_dir()
    peers_file = peers_file or default_peers_path()
    watch_dir = None if no_publish_watch else (publish_watch_dir or default_watch_dir())
    budget = _parse_size(replication_budget) if replication_budget is not None \
        else replication.DEFAULT_REPLICATION_BUDGET
    asyncio.run(net.serve(
        ident, feed_dir, peers_file, relay=not no_relay,
        db_path=ctx.obj["db_path"], enable_lan_discovery=not no_lan_discovery,
        enable_wan_discovery=wan_discovery, publish_watch_dir=watch_dir,
        replicate=not no_replicate, replication_budget=budget, debug=debug_logging,
        **({"wan_discovery_port": wan_port} if wan_port else {}),
        **({"wan_public_port": fixed_port} if fixed_port else {}),
        **({"wan_auto_port": True} if auto_port else {}),
    ))


def _parse_size(value: str) -> int:
    """Parse a human size like `500MB`, `2gb`, `750000`, `0` into bytes.
    Rejected loudly rather than guessed, the same posture as _parse_public_port."""
    v = value.strip().lower().replace(" ", "").replace("i", "")  # accept MiB as MB
    if not v:
        raise click.ClickException("empty --replication-budget")
    units = [("tb", 1024**4), ("gb", 1024**3), ("mb", 1024**2), ("kb", 1024),
             ("t", 1024**4), ("g", 1024**3), ("m", 1024**2), ("k", 1024), ("b", 1)]
    for suffix, mult in units:
        if v.endswith(suffix):
            try:
                return int(float(v[:-len(suffix)]) * mult)
            except ValueError:
                break
    try:
        return int(v)
    except ValueError:
        raise click.ClickException(
            f"not a size: {value!r} -- use a number of bytes, or e.g. 500MB, 2GB") from None


def _parse_public_port(value: str | None) -> tuple[bool, int | None]:
    """`--public-port` takes a number or the word `auto`.

    Rejected loudly rather than ignored: a typo here means silently publishing
    the wrong port (or none), and the symptom is nobody ever arriving -- the
    single least diagnosable failure this program has.
    """
    if value is None:
        return False, None
    text = str(value).strip().lower()
    if text == "auto":
        return True, None
    try:
        port = int(text)
    except ValueError:
        raise click.BadParameter(f"expected a port number or 'auto', got {value!r}") from None
    if not 1 <= port <= 65535:
        raise click.BadParameter(f"port out of range: {port}")
    return False, port


@node.command("doctor")
@click.option("--announce/--no-announce", default=False, show_default=True,
              help="Also publish this node to the DHT swarm while diagnosing.")
@click.option("--json", "as_json", is_flag=True,
              help="Emit the full diagnostic report as JSON (used by the GUI).")
@click.option("--public-port", default=None, type=int,
              help="Test a forwarded port: announce it and check whether a fresh lookup "
                   "can then find this node. Implies --announce.")
def node_doctor(announce: bool, as_json: bool, public_port: int | None) -> None:
    """Diagnose internet-wide (DHT) peer discovery, step by step.

    Internet discovery is the one part of roastmesh that can fail completely
    while looking like it is running: the lookup happens in a background task,
    a failed round leaves no trace, and "no peers" is indistinguishable from
    "announced into the void". This prints what actually happened -- which
    bootstrap routers answered, what our external address looks like from
    outside, how close to the swarm the lookup converged, how many forged
    nodes were turned away, and whether anyone can actually find us.
    """
    import hashlib

    from roastmesh.dht import DhtClient, LookupStats, bep42_valid, load_node_cache, save_node_cache
    from roastmesh.wan_discovery import (
        DEFAULT_DHT_BOOTSTRAP,
        SWARM_INFO_HASH,
        WARM_GOOD_NODES,
        _resolve_named,
        default_state_path,
        diagnostics_payload,
        double_nat_verdict,
        needs_public_port,
        external_address,
    )

    announce_now = announce or public_port is not None

    async def run() -> None:
        ident, _created = load_or_create_identity()
        state_path = default_state_path()
        state = load_node_cache(state_path)

        named = await _resolve_named(DEFAULT_DHT_BOOTSTRAP)
        resolved = [addr for _h, addr in named if addr is not None]
        client = await DhtClient.bind(
            port=0, own_id=hashlib.sha1(bytes.fromhex(ident.public_key_hex)).digest())
        routers: list[dict] = []
        try:
            for host, addr in named:
                if addr is None:
                    routers.append({"host": host, "addr": None, "ok": False})
                    continue
                # bootstrap_ping, not ping: a reply should seed the routing
                # table, which is what the lookup below wants to start from.
                ok = await client.bootstrap_ping(addr, timeout=4.0)
                routers.append({"host": host, "addr": f"{addr[0]}:{addr[1]}", "ok": ok})
            unresolved = sum(1 for _h, addr in named if addr is None)
            reachable = sum(1 for r in routers if r["ok"])

            stats = LookupStats()
            peers: set = set()
            if reachable or state:
                seeds = list(dict.fromkeys([*resolved, *state]))
                peers = await client.discover_and_announce_peers(
                    SWARM_INFO_HASH, seeds, seed_ids=dict(state),
                    announce=announce_now, stats=stats, public_port=public_port,
                )
                state.update({n.addr: n.id for n in client.routing_table.good_nodes()})
                save_node_cache(state_path, state)

            external, nat, votes = external_address(client)
            readback: bool | None = None
            if announce_now and stats.announced > 0 and external is not None:
                # The only question that matters, asked directly: having just
                # published ourselves, does a fresh lookup find us? With a
                # forwarded port that is the address we published, not the one
                # we were seen from -- those differ, which is the whole point.
                published = (external[0], public_port) if public_port else external
                readback = published in await client.discover_and_announce_peers(
                    SWARM_INFO_HASH, list(dict.fromkeys([*resolved, *state])),
                    seed_ids=dict(state), announce=False, stats=LookupStats(),
                )

            # Ask the router what it thinks our public address is. Read-only:
            # this is the diagnostic command, so it looks and does not touch --
            # no mapping is created here even though the same protocol could.
            router_ip = await _ask_router_for_external_ip()

            report = diagnostics_payload(
                client, stats, info_hash=SWARM_INFO_HASH, external=external, nat=nat,
                votes=votes, warm=len(client.routing_table.good_nodes()) >= WARM_GOOD_NODES,
                readback=readback, addrs=peers, public_port=public_port,
                router_external_ip=router_ip,
            )
            report["identity"] = ident.public_key_hex
            report["state_path"] = str(state_path)
            report["state_nodes"] = len(state)
            report["bootstrap"] = routers
            report["bootstrap_unresolved"] = unresolved
            report["announced_this_run"] = announce_now

            if as_json:
                click.echo(json.dumps(report))
                return
            _print_doctor_report(report)
        finally:
            client.close()

    asyncio.run(run())


async def _ask_router_for_external_ip() -> str | None:
    """What the router says our public address is, if one will tell us.

    UPnP is the only one of the three port-mapping protocols with a "what is
    my public address" call, and the answer diagnoses something nothing else
    can -- see wan_discovery.double_nat_verdict.
    """
    import asyncio as _asyncio

    from roastmesh import upnp

    def _look() -> str | None:
        igd = upnp.discover()
        return upnp.get_external_ip(igd) if igd is not None else None

    try:
        return await _asyncio.wait_for(_asyncio.to_thread(_look), 15.0)
    except Exception:  # noqa: BLE001 -- no router is an ordinary answer
        return None


def _print_doctor_report(r: dict) -> None:
    """The human rendering of `node doctor`'s report -- same data the GUI's
    Network diagnostics panel shows, same keys, one source (see
    wan_discovery.diagnostics_payload)."""
    click.echo(f"this node: {r['identity']}")
    click.echo(f"dht node id: {r['node_id']}"
               f"{'  (BEP 42 verified)' if r['node_id_bep42'] else ''}")
    click.echo(f"node state: {r['state_nodes']} known-good DHT node(s) at {r['state_path']}")

    click.echo("\nbootstrap routers:")
    for row in r["bootstrap"]:
        if row["addr"] is None:
            click.echo(f"  {row['host']:26} {'-':>15}  did not resolve")
            continue
        addr = row["addr"].rsplit(":", 1)[0]
        click.echo(f"  {row['host']:26} {addr:>15}  {'ok' if row['ok'] else 'no reply'}")
    if r["bootstrap_unresolved"] == len(r["bootstrap"]):
        click.echo("  none of the router names resolved -- this machine's DNS is not\n"
                   "  answering for public names. That is a DNS problem, not a DHT one;\n"
                   "  roastmesh falls back to known addresses for the two live routers,\n"
                   "  so discovery can still work, but everything else on this machine\n"
                   "  that needs DNS will be broken too.")
    elif r["bootstrap_unresolved"]:
        click.echo(f"  ({r['bootstrap_unresolved']} did not resolve)")
    if not any(row["ok"] for row in r["bootstrap"]) and not r["state_nodes"]:
        click.echo("\nno bootstrap router answered and no known nodes -- "
                   "the DHT is unreachable from this network.")
        return

    click.echo("\nthis node, seen from outside:")
    if r["external_ip"] is None:
        click.echo(f"  address: unknown ({r['ip_votes']} node(s) reported one; "
                   "need more agreement)")
    else:
        click.echo(f"  address: {r['external_ip']}:{r['external_port']}  "
                   f"({r['ip_votes']} independent report(s) agree)")
        if r["nat"] == "symmetric":
            click.echo("  WARNING: your NAT gives a different port to each destination "
                       "(symmetric NAT or carrier-grade NAT). Other nodes cannot send "
                       "you a first packet, so internet discovery cannot work here -- "
                       "this is a network limitation, not a DHT fault. LAN discovery "
                       "and pasted tickets still work.")
        else:
            click.echo("  NAT: consistent mapping -- your address is stable, which is the "
                       "half\n       of the problem this can measure. Whether a stranger's "
                       "first packet is\n       accepted depends on your router's filtering, "
                       "which nothing here can\n       tell you: a stable mapping that still "
                       "drops unsolicited packets is\n       common, and was exactly the case "
                       "on the machine this was tested from.")

    verdict = r.get("double_nat")
    if verdict == "double-nat":
        click.echo(f"  your router says its own public address is "
                   f"{r['router_external_ip']}, which is a private one --\n"
                   "  so it is behind another NAT as well (carrier-grade NAT). No port\n"
                   "  you forward on it can be reached from the internet. Only your ISP\n"
                   "  can change that; a VPN offering port forwarding is the usual way out.")
    elif verdict == "agrees":
        click.echo(f"  your router agrees: {r['router_external_ip']}")
    elif verdict == "unconfirmed":
        click.echo(f"  your router says {r['router_external_ip']} (nothing to compare it "
                   "with yet --\n  the DHT has not settled on an address)")
    elif verdict == "disagrees":
        # Measured on the development machine: the ISP changed its address, the
        # router knew immediately and the DHT tally was still carrying the old
        # one. The router is authoritative for its own WAN and updates first;
        # the DHT is what peers actually see and needs a fresh quorum. So this
        # is usually a tally catching up, not two routes out -- and it is the
        # reason the router's answer is reported rather than acted on.
        click.echo(f"  NOTE: your router says {r['router_external_ip']}, the DHT says "
                   f"{r['external_ip']}.\n"
                   "  Two usual causes: your traffic leaves through something other than\n"
                   "  that router (a VPN, most often), or your address changed and the "
                   "DHT's\n  view has not caught up. Either way peers reach you at what "
                   "the DHT\n  reports -- forwarding a port on the router only helps in "
                   "the second case.")

    t = r["routing_table"]
    click.echo(f"\nrouting table: {t['good']} good of {t['total']} "
               f"({t['verified']} BEP 42 verified)"
               f"{'' if r['warm'] else '  -- still warming up'}")

    lk = r["lookup"]
    closest = "none" if lk["closest_bits"] is None else f"2^{lk['closest_bits']}"
    click.echo("\nswarm lookup:")
    click.echo(f"  {lk['rounds']} rounds, {lk['replied']}/{lk['queried']} replied, "
               f"closest {closest}, announced to {lk['announced']} "
               f"({lk['no_token']} gave no token)")
    click.echo(f"  turned away: {lk['rejected_impossible_proximity']} forged-proximity, "
               f"{lk['rejected_bep42']} failing BEP 42, {lk['rejected_martian']} unroutable")
    if lk["closest_bits"] is not None and lk["closest_bits"] > 150:
        click.echo("  WARNING: the lookup never got near the swarm -- it is not converging.")
    if lk["closest_bits"] is not None and lk["closest_bits"] < 120:
        click.echo("  WARNING: the lookup reached a distance no honest node can occupy. "
                   "Something is forging node IDs and the filters did not catch it.")

    if r["announce_set"]:
        click.echo("\n  closest nodes reached (these are what store and serve us):")
        for row in r["announce_set"]:
            mark = "verified" if row["bep42"] is True else (
                "exempt" if row["bep42"] is None else "UNVERIFIED")
            click.echo(f"    {row['addr']:>22}  2^{row['bits']:<4} {mark}")
        if any(row["bep42"] is False for row in r["announce_set"]):
            click.echo("  WARNING: an unverified node is in the set we publish to.")

    if r["readback"] is True:
        click.echo("\n  read-back: OK -- a fresh lookup found this node's own address, "
                   "so other nodes can find it too.")
    elif r["readback"] is False:
        click.echo("\n  read-back: FAILED -- we announced, but a fresh lookup could not "
                   "find our own address. Other nodes will not find us either.")

    if r.get("needs_public_port"):
        _print_public_port_advice(r)

    if r["peers"]:
        click.echo(f"\n  {len(r['peers'])} address(es) advertised on the roastmesh swarm:")
        for addr in r["peers"]:
            click.echo(f"    {addr}")
        click.echo("  (each still has to pass the roastmesh handshake before it counts "
                   "as a peer)")
    else:
        click.echo("\n  no roastmesh peers currently advertised. If another node is "
                   "serving with --wan-discovery right now, re-run in a minute -- "
                   "announcements take a round to propagate.")


def _print_public_port_advice(r: dict) -> None:
    """Tell the user the one thing that will actually fix this.

    Reached when the address we publish is provably useless: a NAT that hands
    out a different port per destination, or an announce we could not find
    afterwards. Neither is fixable from inside the DHT, and without this the
    report just says "not findable" and leaves the user nowhere.
    """
    port = r.get("public_port")
    if r.get("double_nat") == "double-nat":
        click.echo("\n  WHAT TO DO: nothing here will help.")
        click.echo("  Your router is itself behind your ISP's NAT, so there is no port on\n"
                   "  it that anyone outside can reach -- forwarding one changes nothing.\n"
                   "  A VPN that offers port forwarding is the usual way out; roastmesh\n"
                   "  still finds others and syncs with them meanwhile.")
        return
    click.echo("\n  WHAT TO DO: this node needs a forwarded port.")
    if r["nat"] == "symmetric":
        click.echo("  Your NAT gives every destination a different port, so the port "
                   "others\n  see is never the port they can reach you on. Publishing it "
                   "is useless.")
    if port is not None:
        click.echo(f"  Port {port} is configured but the read-back still failed, so it is "
                   "not\n  actually open. Check the forward really points at this machine, "
                   "and that\n  roastmesh is listening on it (--wan-port).")
        return
    click.echo("  Get a port forwarded to this machine, then run:")
    click.echo("      roastmesh node serve --wan-discovery --wan-port N --public-port N")
    click.echo("  where N is the forwarded port. Test it first with:")
    click.echo("      roastmesh node doctor --public-port N")
    click.echo("  Where N comes from:")
    click.echo("   * your router's port-forwarding page (forward a UDP port to this "
               "machine);")
    click.echo("   * or your VPN, if it offers port forwarding -- Private Internet "
               "Access\n     does, and `piactl get portforward` prints the port it gave "
               "you;")
    click.echo("   * on carrier-grade NAT you cannot forward anything yourself, and a "
               "VPN with\n     port forwarding is the usual way out.")
    click.echo("  Without one, this node can still find others and sync with them -- it "
               "just\n  cannot be found first. Pasted tickets and LAN discovery are "
               "unaffected.")


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
        # ip: the peer's public address parsed from its ticket (None if LAN/relay
        # only) -- the GUI turns it into a country flag. Additive; text output unchanged.
        click.echo(json.dumps([{**asdict(p), "ip": public_ip_from_ticket(p.ticket)} for p in peers]))
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
    try:
        report = asyncio.run(net.sync_with_peer(
            ticket, ident, ctx.obj["peer_feeds_root"], ctx.obj["peers_file"], added_via=added_via,
        ))
    except Exception as exc:  # noqa: BLE001 -- iroh raises its own error type
        # A peer that is offline, unreachable, or behind a NAT we cannot punch
        # is an ordinary outcome, not a crash. Left unhandled this printed a
        # PyInstaller traceback ending in a bare `iroh.iroh_ffi.IrohError` --
        # observed on a Raspberry Pi whose DNS was dead, so iroh could not
        # resolve a relay to connect through. Nothing in that output tells the
        # user what happened or what to do about it.
        raise click.ClickException(
            f"could not connect to that peer: {type(exc).__name__}"
            f"{': ' + str(exc) if str(exc) else ''}\n"
            "They may be offline, or neither side can reach the other. If this "
            "machine cannot resolve hostnames, connecting through a relay will "
            "always fail -- check DNS first (`roastmesh node doctor` says so "
            "outright when no bootstrap name resolves)."
        ) from exc
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
    # Record who holds what for replication (no third-party feeds are pulled on
    # a manual `peer sync` -- that is the serving node's background job).
    net.record_sync_replication(conn, report, Path(ctx.obj["peer_feeds_root"]))
    if report.held_feeds:
        click.echo(f"peer advertises {len(report.held_feeds)} feed(s) it holds")


def _fetch_stub_on_demand(ctx: click.Context, feed_pubkey: str) -> bool:
    """Re-materialize an evicted stub's bytes: find a known holder of its feed
    that we have a ticket for, and pull that feed from them (also_pull). The
    same verified path any replication uses -- forgery is impossible, the
    signature is the author's. Returns whether the bytes are now local."""
    peers_file = ctx.obj.get("peers_file") or default_peers_path()
    peer_feeds_root = ctx.obj.get("peer_feeds_root") or default_peer_feeds_root()
    conn = connect(ctx.obj["db_path"])
    holders = repo.known_holders(conn, feed_pubkey)
    conn.close()
    if not holders:
        return False
    by_pubkey = {pr.feed_pubkey_hex: pr for pr in load_peers(peers_file) if pr.feed_pubkey_hex}
    ident, _ = load_or_create_identity()
    for holder in holders:
        peer = by_pubkey.get(holder)
        if peer is None:
            continue  # we know they had it, but have no way to reach them
        try:
            report = asyncio.run(net.sync_with_peer(
                peer.ticket, ident, peer_feeds_root, peers_file,
                added_via=peer.added_via, also_pull=[feed_pubkey],
            ))
        except Exception:  # noqa: BLE001 -- try the next holder
            continue
        if feed_pubkey in report.pulled_feeds:
            conn = connect(ctx.obj["db_path"])
            net.record_sync_replication(conn, report, Path(peer_feeds_root))
            conn.close()
            return True
    return False


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


@peer.command("replication")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON instead of text.")
@click.pass_context
def peer_replication(ctx: click.Context, as_json: bool) -> None:
    """Show what this node is mirroring for the network and what's at risk.

    Held = feeds whose bytes are on disk here (yours and others'); stubs =
    feeds evicted to search-only entries, re-fetched on demand; at-risk =
    feeds we hold that few other reachable peers do -- the ones our copy is
    keeping alive.
    """
    ident, _ = load_or_create_identity()
    own = ident.public_key_hex
    digest = held_feeds_digest(default_feed_dir(), own, ctx.obj["peer_feeds_root"])
    used = sum(d["total_bytes"] for d in digest)

    conn = connect(ctx.obj["db_path"])
    known = repo.load_known_feeds(conn)
    holder_counts = repo.feed_holder_counts(conn, exclude_holder=own)
    conn.close()
    held = [r["feed_pubkey"] for r in known if r["held_local"]]
    stubs = [r["feed_pubkey"] for r in known if not r["held_local"]]
    at_risk = sorted(
        (pk for pk in held if pk != own and holder_counts.get(pk, 0) <= 1),
        key=lambda pk: holder_counts.get(pk, 0),
    )

    if as_json:
        click.echo(json.dumps({
            "own_feed": own, "used_bytes": used,
            "default_budget_bytes": replication.DEFAULT_REPLICATION_BUDGET,
            "held": len(digest), "stubs": len(stubs), "known": len(known),
            "at_risk": [{"feed": pk, "other_holders": holder_counts.get(pk, 0)} for pk in at_risk],
        }))
        return

    mb = used / (1024 * 1024)
    click.echo(f"mirroring {len(digest)} feed(s) on disk ({mb:.1f} MB used); "
               f"default budget {replication.DEFAULT_REPLICATION_BUDGET // (1024*1024)} MB")
    click.echo(f"ledger: {len(known)} feed(s) known, {len(stubs)} kept as search-only stub(s)")
    if at_risk:
        click.echo(f"at risk -- few other holders, our copy matters ({len(at_risk)}):")
        for pk in at_risk[:20]:
            click.echo(f"  {pk[:16]}...  other holders: {holder_counts.get(pk, 0)}")
    else:
        click.echo("no held feed is at risk -- every one has other reachable holders")


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


@main.command("update")
@click.option("--check", "check_only", is_flag=True,
              help="Only report whether a newer release exists; do not install.")
@click.option("--json", "as_json", is_flag=True, help="With --check, emit the result as JSON.")
@click.option("--yes", is_flag=True, help="Install without the confirmation prompt.")
@click.option("--relaunch-pid", type=int, default=None, hidden=True,
              help="Internal: PID the Windows installer helper waits to exit before installing.")
def update(check_only: bool, as_json: bool, yes: bool, relaunch_pid: int | None) -> None:
    """Check for a newer roastmesh release and update this installation in place."""
    from roastmesh import updater

    current = roastmesh.__version__
    info = updater.check_latest(current)
    supported = updater.is_supported()

    if check_only:
        latest = info.latest_version if info else current
        page = info.page_url if info else updater.RELEASES_PAGE
        is_newer = bool(info and info.is_newer)
        if as_json:
            click.echo(json.dumps({
                "current": current, "latest": latest, "is_newer": is_newer,
                "supported": supported, "page_url": page, "checked": info is not None,
            }))
        elif info is None:
            click.echo("could not check for updates (offline?)")
        elif is_newer:
            click.echo(f"update available: {latest} (you have {current})")
        else:
            click.echo(f"up to date ({current})")
        return

    if info is None:
        raise click.ClickException("could not check for updates -- are you online?")
    if not info.is_newer and not yes:
        click.echo(f"already up to date ({current}).")
        return
    if not supported:
        click.echo("auto-update isn't supported for this installation.")
        click.echo(f"download the latest release from: {info.page_url}")
        raise SystemExit(2)
    if not yes:
        click.confirm(f"Update from {current} to {info.latest_version} now?", abort=True)
    try:
        updater.perform_update(progress=lambda m: click.echo(m), wait_pid=relaunch_pid)
    except updater.UpdateError as exc:
        click.echo(f"update failed: {exc}")
        click.echo(f"download the latest release from: {exc.page_url}")
        raise SystemExit(2) from exc
    click.echo("done.")


if __name__ == "__main__":
    main()
