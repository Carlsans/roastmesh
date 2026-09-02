import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from roastmesh.peers import Peer, load_peers, peer_from_dict, prune_stale, save_peers, upsert_peer

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


def test_peer_from_dict_drops_unknown_fields() -> None:
    now = datetime.now(timezone.utc).isoformat()
    peer = peer_from_dict({
        "ticket": TICKET_A, "feed_pubkey_hex": "abc123", "first_seen": now, "last_seen": now,
        "added_via": "manual", "a_future_field_this_version_does_not_know_about": "surprise",
    })
    assert peer.ticket == TICKET_A
    assert not hasattr(peer, "a_future_field_this_version_does_not_know_about")


def test_load_peers_tolerates_a_dict_with_an_unknown_field(tmp_path: Path) -> None:
    """Peer(**d) landmine, from disk: a peers.json written by a newer
    version of roastmesh that added a field this version doesn't know about
    must not raise TypeError and abort the whole load."""
    path = tmp_path / "peers.json"
    now = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps([
        {"ticket": TICKET_A, "feed_pubkey_hex": "abc123", "first_seen": now, "last_seen": now,
         "added_via": "manual", "a_future_field_this_version_does_not_know_about": "surprise"},
    ]))

    loaded = load_peers(path)

    assert len(loaded) == 1
    assert loaded[0].ticket == TICKET_A
    assert loaded[0].feed_pubkey_hex == "abc123"


def test_a_ticket_is_parsed_once_however_often_it_is_merged() -> None:
    """The peer merge is quadratic, so what it does per comparison decides
    whether a sync is instant or unusable.

    upsert_peer resolves every *stored* peer's node id on each insert, and
    the sync path inserts once per *gossiped* peer -- so a merge performs
    O(n^2) ticket resolutions. Ticket parsing goes through the iroh FFI at
    ~120us a call, which on a real 764-peer list measured 583,696 parses and
    ~60 seconds of pure CPU for a sync that transferred nothing. It ran on
    every sync, on every node, and scaled with the size of the network.

    Found by timing a Windows<->Pi sync (104s) against a LAN sync (100s):
    identical, so it was never the transport. `time` then showed 96% CPU.

    This pins the fix rather than the symptom -- a timing assertion would be
    flaky, but "each distinct ticket is resolved at most once" is exact.
    """
    from roastmesh import peers as peers_mod

    peers_mod.node_id_from_ticket.cache_clear()
    tickets = [f"ticket-{i}" for i in range(60)]
    merged: list[Peer] = []
    for ticket in tickets:
        merged = upsert_peer(merged, _peer(ticket))
    # Re-merging the same list is what a gossip exchange actually does.
    for ticket in tickets:
        merged = upsert_peer(merged, _peer(ticket))

    info = peers_mod.node_id_from_ticket.cache_info()
    assert len(merged) == len(tickets)
    assert info.misses == len(tickets), (
        f"expected one parse per distinct ticket, got {info.misses} for "
        f"{len(tickets)} tickets -- the memoisation is not holding"
    )
    # Without the cache this is where the ~583k parses came from.
    assert info.hits > len(tickets) ** 2 // 2
