"""Well-known bootstrap peers, shipped in the binary (ARCHITECTURE.md's Peer
Discovery section, same pattern as BitTorrent's `router.bittorrent.com`):
any one working entry recovers a fresh install's whole peer list via gossip.

Empty by design: a real bootstrap node is a maintainer running an always-on
`roastnet node serve` somewhere (a VPS, a Pi) and publishing its ticket here
-- that's infrastructure/ops, not something to fabricate in a coding
session. Until one exists, `roastnet peer bootstrap` is a documented no-op
and manual peer entry (`roastnet peer add <ticket>`) is how a node joins.
"""
from __future__ import annotations

BOOTSTRAP_TICKETS: list[str] = []
