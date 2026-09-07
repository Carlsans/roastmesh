"""Well-known bootstrap peers, shipped in the binary (ARCHITECTURE.md's Peer
Discovery section, same pattern as BitTorrent's `router.bittorrent.com`):
any one working entry recovers a fresh install's whole peer list via gossip.

Empty by design: a real bootstrap node is a maintainer running an always-on
`roastmesh node serve` somewhere (a VPS, a Pi) and publishing its ticket here
-- that's infrastructure/ops, not something to fabricate in a coding
session. Until one exists, `roastmesh peer bootstrap` is a documented no-op
and manual peer entry (`roastmesh peer add <ticket>`) is how a node joins.

`moduloinfo.ca` (below) is that node for the *WAN-discovery* rendezvous path
-- see RendezvousHost. It answers the same "hello" datagram
(roastmesh.hello) LAN/WAN discovery already exchange, sent directly instead
of found via the public DHT -- which is what makes it useful: a live
rendezvous host answers in well under a second, where the public DHT can
take minutes to converge on a cold start (see wan_discovery.py's own
bootstrap/lookup-interval constants).
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import roastmesh
from roastmesh import updater
from roastmesh.paths import data_dir
from roastmesh.wan_discovery import WAN_PORT

BOOTSTRAP_TICKETS: list[str] = []


@dataclass
class RendezvousHost:
    """One always-on roastmesh node worth dialling directly for `hello` on
    startup, instead of waiting for the public DHT to find it.

    `ip` is an optional literal fallback for when DNS itself is
    unavailable/blocked -- same idea as
    wan_discovery.DHT_BOOTSTRAP_FALLBACK_IPS.
    """
    host: str
    ip: str | None
    port: int


# Baked into the binary so it works even with GitHub itself unreachable --
# this is what "points directly at this network at all times" means. The
# GitHub-hosted list below (RENDEZVOUS_HOSTS_URL) can only ever ADD to or
# rotate this, never take it away, because effective_rendezvous_hosts falls
# back here when both a fetch and the on-disk cache come up empty.
DEFAULT_RENDEZVOUS_HOSTS: list[RendezvousHost] = [
    RendezvousHost(host="moduloinfo.ca", ip=None, port=WAN_PORT),
]

# Same repo/branch install.sh already pulls release assets from -- see
# BOOTSTRAP_NODES at the repo root for the file this fetches.
RENDEZVOUS_HOSTS_URL = f"https://raw.githubusercontent.com/{updater.REPO}/master/BOOTSTRAP_NODES"

_FETCH_TIMEOUT_S = 5.0


def parse_rendezvous_file(text: str) -> list[RendezvousHost]:
    """Parse BOOTSTRAP_NODES's plain-text format: one host per line,
    `host[,literal_ip[,port]]`; blank lines and `#` comments ignored.

    Tolerant of a malformed line (skipped, not fatal) since this file is
    fetched over the network and must never be allowed to break startup.
    """
    hosts: list[RendezvousHost] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        host = parts[0]
        if not host:
            continue
        ip = parts[1] if len(parts) > 1 and parts[1] else None
        port = WAN_PORT
        if len(parts) > 2 and parts[2]:
            try:
                port = int(parts[2])
            except ValueError:
                continue
        hosts.append(RendezvousHost(host=host, ip=ip, port=port))
    return hosts


def default_rendezvous_cache_path() -> Path:
    return data_dir() / "rendezvous_hosts.json"


def load_cached_rendezvous_hosts(path: Path | None = None) -> list[RendezvousHost]:
    """Never raises: a missing or corrupt cache is just "nothing cached
    yet", the same convention device_sync.load_state uses."""
    path = path if path is not None else default_rendezvous_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    hosts: list[RendezvousHost] = []
    for item in data.get("hosts", []):
        try:
            hosts.append(RendezvousHost(host=str(item["host"]), ip=item.get("ip"), port=int(item["port"])))
        except (KeyError, TypeError, ValueError):
            continue
    return hosts


def save_cached_rendezvous_hosts(hosts: list[RendezvousHost], path: Path | None = None) -> None:
    path = path if path is not None else default_rendezvous_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hosts": [{"host": h.host, "ip": h.ip, "port": h.port} for h in hosts]}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fetch_sync(timeout: float) -> list[RendezvousHost] | None:
    try:
        req = urllib.request.Request(
            RENDEZVOUS_HOSTS_URL, headers={"User-Agent": f"roastmesh/{roastmesh.__version__}"},
        )
        # Reuses updater's frozen-build CA-bundle fix rather than duplicating
        # it -- see updater._ssl_context's own docstring.
        with urllib.request.urlopen(req, timeout=timeout, context=updater._ssl_context()) as resp:  # noqa: S310
            text = resp.read().decode("utf-8")
    except Exception:  # noqa: BLE001 -- a failed fetch must be silent, never fatal
        return None
    return parse_rendezvous_file(text) or None


async def fetch_rendezvous_hosts(*, timeout: float = _FETCH_TIMEOUT_S) -> list[RendezvousHost] | None:
    """Fetch and parse RENDEZVOUS_HOSTS_URL off the event loop.

    Returns None (never an empty list) on any failure -- network error,
    timeout, or a file that parsed to zero hosts -- so callers can tell
    "nothing fetched" from "fetched, intentionally empty" and fall back to
    the cache/default accordingly.
    """
    return await asyncio.to_thread(_fetch_sync, timeout)


def effective_rendezvous_hosts(
    cached: list[RendezvousHost], fetched: list[RendezvousHost] | None,
) -> list[RendezvousHost]:
    """Precedence: a fresh fetch wins; otherwise the last cached copy;
    otherwise the hardcoded default.

    `moduloinfo.ca` stays reachable through this function even with GitHub
    (and the on-disk cache) both unavailable, because it's baked into
    DEFAULT_RENDEZVOUS_HOSTS.
    """
    if fetched:
        return fetched
    if cached:
        return cached
    return list(DEFAULT_RENDEZVOUS_HOSTS)
