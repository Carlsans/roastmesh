"""Known-peer bookkeeping: manual entry, gossip merge, liveness pruning.

Pure local-file logic, no networking here -- net.py calls into this after a
sync exchange. Storage is a plain JSON list (`peers.json`), same pattern as
`identity.json`: small, human-inspectable, no need for a SQLite table.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import iroh
from roastmesh.paths import data_dir


@dataclass
class Peer:
    ticket: str
    feed_pubkey_hex: str | None
    first_seen: str
    last_seen: str
    added_via: str  # "manual" | "gossip" | "bootstrap"


def default_peers_path() -> Path:
    return data_dir() / "peers.json"


def peer_from_dict(d: dict) -> Peer:
    """Build a `Peer` from an untrusted dict -- one loaded from disk, or one
    handed over the wire by a peer during sync (net.sync_with_peer's
    get_peers gossip). Filters to this version's own known dataclass fields
    first: a plain `Peer(**d)` raises TypeError the moment a newer peer (or
    a future version of this file) adds a field this version doesn't know
    about yet, which would abort a whole peers.json load or an entire sync
    over one extra, harmless key. Unknown keys are just dropped."""
    known = {f.name for f in fields(Peer)}
    return Peer(**{k: v for k, v in d.items() if k in known})


def load_peers(path: Path | None = None) -> list[Peer]:
    path = path or default_peers_path()
    if not path.exists():
        return []
    return [peer_from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))]


def save_peers(peers: list[Peer], path: Path | None = None) -> None:
    path = path or default_peers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(p) for p in peers], indent=2), encoding="utf-8")


# Cached because upsert_peer calls this once per *stored* peer on every
# insert, and the sync path inserts once per *gossiped* peer -- so merging a
# peer list costs O(n^2) of whatever this function does. Measured on a real
# 764-peer list: 120us per parse x 583,696 parses = ~60s of pure CPU for a
# sync that transferred nothing, on every sync, on every node. A ticket is
# an immutable string and its embedded id cannot change, so the parse is
# safe to memoise; that turns all but the first parse of each distinct
# ticket into a dict lookup and the same merge into well under a second.
#
# The merge is still quadratic, just in cheap operations now. If peer lists
# ever reach tens of thousands, upsert_peer wants a keyed index instead of
# a rescan -- this makes that a scaling question rather than a live defect.
@lru_cache(maxsize=8192)
def node_id_from_ticket(ticket: str) -> str | None:
    """The EndpointId embedded in a ticket -- stable identity to dedup peers
    by, since a peer's direct addresses (also embedded in the ticket) can
    change between runs while its identity doesn't. Ticket parsing is
    synchronous and doesn't need the uniffi event loop wired up (confirmed
    by iroh-ffi's own test_endpoint_ticket_rejects_garbage, a plain `def`
    test)."""
    try:
        return str(iroh.EndpointTicket.from_string(ticket).endpoint_addr().id())
    except Exception:
        return None


@lru_cache(maxsize=8192)
def public_ip_from_ticket(ticket: str) -> str | None:
    """The first public IPv4 among a ticket's direct addresses, or None.

    A ticket pins the addresses a peer advertised when it minted the ticket; a
    WAN-reachable peer's include its public IP, a LAN-only or relay-only peer's
    do not (so those get no country flag). Cached the same way node_id_from_ticket
    is -- a ticket is an immutable string."""
    import ipaddress
    try:
        addrs = iroh.EndpointTicket.from_string(ticket).endpoint_addr().direct_addresses()
    except Exception:
        return None
    for a in addrs or []:
        host = str(a).rsplit(":", 1)[0].strip("[]")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if ip.version == 4 and not (ip.is_private or ip.is_loopback
                                    or ip.is_link_local or ip.is_reserved):
            return str(ip)
    return None


def upsert_peer(peers: list[Peer], new: Peer) -> list[Peer]:
    """Merge `new` into `peers`, deduping by node id. On conflict, `ticket`/
    `last_seen`/`feed_pubkey_hex` always update to the newer values, but a
    gossip-sourced update never downgrades an existing manual/bootstrap
    `added_via` tag -- gossip is the least-trusted way a peer got into the
    list."""
    new_key = node_id_from_ticket(new.ticket) or new.ticket
    result = []
    replaced = False
    for existing in peers:
        key = node_id_from_ticket(existing.ticket) or existing.ticket
        if key != new_key:
            result.append(existing)
            continue
        added_via = existing.added_via if existing.added_via != "gossip" and new.added_via == "gossip" \
            else new.added_via
        result.append(Peer(
            ticket=new.ticket,
            feed_pubkey_hex=new.feed_pubkey_hex or existing.feed_pubkey_hex,
            first_seen=existing.first_seen,
            last_seen=new.last_seen,
            added_via=added_via,
        ))
        replaced = True
    if not replaced:
        result.append(new)
    return result


def prune_stale(peers: list[Peer], *, max_age_days: float, now: datetime | None = None) -> list[Peer]:
    """Drop peers not seen within `max_age_days`. Only ever touches the peer
    list -- already-replicated feed data in the SQLite index is untouched
    (ARCHITECTURE.md: "drop peers unreachable for N days, but keep their
    replicated data")."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    return [p for p in peers if datetime.fromisoformat(p.last_seen) >= cutoff]


# The peer list has one growth source that time-based pruning cannot bound:
# a peer's get_peers response is merged wholesale, and last_seen is
# attacker-declared, so a single hostile peer can gossip an unbounded number
# of freshly-timestamped entries that prune_stale will never drop. Found by
# an adversarial pass: 4,000 injected peers grew peers.json to 1.3 MB and
# the O(n) merge to over a second; extrapolated, tens of thousands make a
# sync take minutes. A hard cap turns "grows without limit" into a bounded
# cost. Sized well above any real network a hobbyist tool will see.
MAX_PEERS = 2000


def cap_peers(peers: list[Peer], *, limit: int = MAX_PEERS,
              now: datetime | None = None) -> list[Peer]:
    """Bound the peer list at `limit`, evicting the least valuable first.

    Eviction order, worst first: gossip before manually-added/bootstrap
    (gossip is the only source an attacker can flood, and the least trusted
    -- ARCHITECTURE.md), then oldest last_seen before newest. So a flood of
    freshly-gossiped strangers can never push out the handful of peers a user
    added by hand, no matter how recent the flood claims to be.
    """
    if len(peers) <= limit:
        return peers

    def _last_seen(p: Peer) -> datetime:
        try:
            return datetime.fromisoformat(p.last_seen)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    # Sort best-to-keep first: trusted source, then most recently seen.
    ranked = sorted(
        peers,
        key=lambda p: (p.added_via == "gossip", -_last_seen(p).timestamp()),
    )
    return ranked[:limit]
