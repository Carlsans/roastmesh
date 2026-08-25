"""Known-peer bookkeeping: manual entry, gossip merge, liveness pruning.

Pure local-file logic, no networking here -- net.py calls into this after a
sync exchange. Storage is a plain JSON list (`peers.json`), same pattern as
`identity.json`: small, human-inspectable, no need for a SQLite table.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import iroh


@dataclass
class Peer:
    ticket: str
    feed_pubkey_hex: str | None
    first_seen: str
    last_seen: str
    added_via: str  # "manual" | "gossip" | "bootstrap"


def default_peers_path() -> Path:
    return Path.home() / ".local" / "share" / "roastnet" / "peers.json"


def load_peers(path: Path | None = None) -> list[Peer]:
    path = path or default_peers_path()
    if not path.exists():
        return []
    return [Peer(**d) for d in json.loads(path.read_text())]


def save_peers(peers: list[Peer], path: Path | None = None) -> None:
    path = path or default_peers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(p) for p in peers], indent=2))


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
