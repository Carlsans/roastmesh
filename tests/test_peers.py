from datetime import datetime, timedelta, timezone
from pathlib import Path

from roastmesh.peers import Peer, load_peers, prune_stale, save_peers, upsert_peer

# A syntactically valid Iroh ticket isn't needed for most of these tests --
# node_id_from_ticket() falls back to the raw ticket string when parsing
# fails, and that fallback is exactly what's being exercised here (dedup
# still works even for made-up ticket strings in unit tests).
TICKET_A = "ticket-a"
TICKET_A_MOVED = "ticket-a-new-address"  # same peer, ticket changed (e.g. IP moved)
TICKET_B = "ticket-b"


def _peer(ticket: str, *, added_via: str = "manual", last_seen: str | None = None) -> Peer:
    now = last_seen or datetime.now(timezone.utc).isoformat()
    return Peer(ticket=ticket, feed_pubkey_hex="abc123", first_seen=now, last_seen=now, added_via=added_via)


def test_upsert_adds_new_peer() -> None:
    peers = upsert_peer([], _peer(TICKET_A))
    assert len(peers) == 1
    assert peers[0].ticket == TICKET_A


def test_upsert_updates_existing_peer_in_place() -> None:
    peers = upsert_peer([], _peer(TICKET_A, added_via="manual"))
    peers = upsert_peer(peers, _peer(TICKET_A, added_via="manual", last_seen="2026-06-01T00:00:00+00:00"))
    assert len(peers) == 1
    assert peers[0].last_seen == "2026-06-01T00:00:00+00:00"


def test_upsert_does_not_downgrade_manual_to_gossip() -> None:
    peers = upsert_peer([], _peer(TICKET_A, added_via="manual"))
    peers = upsert_peer(peers, _peer(TICKET_A, added_via="gossip", last_seen="2026-06-01T00:00:00+00:00"))
    assert len(peers) == 1
    assert peers[0].added_via == "manual"
    assert peers[0].last_seen == "2026-06-01T00:00:00+00:00"  # last_seen still updates


def test_upsert_gossip_can_upgrade_to_manual() -> None:
    peers = upsert_peer([], _peer(TICKET_A, added_via="gossip"))
    peers = upsert_peer(peers, _peer(TICKET_A, added_via="manual"))
    assert peers[0].added_via == "manual"


def test_upsert_two_different_peers_both_kept() -> None:
    peers = upsert_peer([], _peer(TICKET_A))
    peers = upsert_peer(peers, _peer(TICKET_B))
    assert {p.ticket for p in peers} == {TICKET_A, TICKET_B}


def test_prune_stale_removes_old_peers_only() -> None:
    now = datetime.now(timezone.utc)
    fresh = _peer(TICKET_A, last_seen=now.isoformat())
    stale = _peer(TICKET_B, last_seen=(now - timedelta(days=45)).isoformat())

    kept = prune_stale([fresh, stale], max_age_days=30, now=now)
    assert [p.ticket for p in kept] == [TICKET_A]


def test_prune_stale_keeps_everything_within_window() -> None:
    now = datetime.now(timezone.utc)
    peers = [_peer(TICKET_A, last_seen=now.isoformat()), _peer(TICKET_B, last_seen=now.isoformat())]
    kept = prune_stale(peers, max_age_days=30, now=now)
    assert len(kept) == 2


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "peers.json"
    peers = [_peer(TICKET_A), _peer(TICKET_B, added_via="gossip")]
    save_peers(peers, path)

    loaded = load_peers(path)
    assert [p.ticket for p in loaded] == [TICKET_A, TICKET_B]
    assert loaded[1].added_via == "gossip"


def test_load_peers_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_peers(tmp_path / "does-not-exist.json") == []
