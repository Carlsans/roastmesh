"""The private folder mirror between a user's own paired devices: the
reconcile engine (pure, offline-testable) plus the network glue that drives
it and the device-pairing handshake over a real connection.

This is the *opposite* trust and mutability model from feed.py's public,
append-only feed -- see the plan this module implements (and
ARCHITECTURE.md's "Device pairing & private sync" section) for the full
comparison. In one line: any file type, full mirror (add/change/**delete**
all propagate), newest write wins, and it is only ever reachable by a device
this identity has explicitly SAS-paired with (devices.is_trusted) -- never
published to, or readable from, the public feed.

Two ALPNs, both branched on by net.serve()'s single endpoint (the same
endpoint the public feed's ALPN already shares, per net.py's own docstring):
`PAIR_ALPN` for the one-time SAS handshake (pairing.py) that adds a device to
the trusted set, `SYNC_ALPN` for the actual folder reconciliation once paired.
Deliberately kept as *two* protocols rather than one: pairing decides *whether*
to trust a key at all, sync decides *what to do* once trust already exists --
conflating them would mean every sync request also had to re-litigate trust
inline, and every pairing attempt would need read/write folder access before
a human has even confirmed anything.

Everything up to and including `reconcile()` is pure and transport-agnostic,
same posture as pairing.py: it operates on plain dicts and never touches a
socket, so the whole reconcile algorithm -- including the tombstone/newest-
wins semantics that make a *delete* propagate correctly -- is unit-testable
with two temp directories and zero network (tests/test_device_sync.py).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import iroh

from roastmesh import devices, net
from roastmesh.devices import Device
from roastmesh.identity import Identity
from roastmesh.lan_discovery import PairingCandidate, discover_pairing_beacons
from roastmesh.pairing import PairResult, run_pairing
from roastmesh.paths import device_sync_state_path

PAIR_ALPN = b"roastmesh/device-pair/0"
SYNC_ALPN = b"roastmesh/device-sync/0"

STATE_VERSION = 1

# A tombstone has to be pruned eventually -- kept forever, device_sync_state.json
# grows without bound on a folder with any real churn. 90 days is generous
# relative to RESYNC_INTERVAL_S (lan_discovery.py, 15 minutes): any paired
# device that's been offline for three months is already well past the point
# where "did I miss a delete" matters less than "did this device come back at
# all". A tombstone pruned locally simply stops being *asserted* here -- it
# does not resurrect the file (nothing recreates it), it just means a *very*
# late-arriving older record for that path would no longer be provably
# overridden, an acceptable trade at that age.
TOMBSTONE_MAX_AGE_DAYS = 90.0

# Reserved for a future per-file version history feature -- not implemented in
# v1, but the name is claimed now (and scan_folder ignores it) so a later
# version can start writing into it without first needing every existing
# client to learn to ignore a brand new top-level directory.
_VERSIONS_DIR_NAME = ".roastmesh-versions"


# --------------------------------------------------------------------------
# Sync state: relpath -> {sha256, size, mtime_ns, deleted, updated_at}
# --------------------------------------------------------------------------

def load_state(path: Path | None = None) -> dict:
    """The persisted local manifest: relpath -> record. Corrupt or missing
    is just "nothing known yet" -- the same posture gui_config.load_config
    takes toward its own file, since a mirror engine should degrade to "scan
    everything fresh" rather than refuse to run."""
    path = path or device_sync_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = data.get("records")
    return records if isinstance(records, dict) else {}


def _prune_old_tombstones(records: dict, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    cutoff = now - TOMBSTONE_MAX_AGE_DAYS * 86400
    return {
        rel: rec for rel, rec in records.items()
        if not (isinstance(rec, dict) and rec.get("deleted") and rec.get("updated_at", 0) < cutoff)
    }


def save_state(records: dict, path: Path | None = None) -> None:
    path = path or device_sync_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    pruned = _prune_old_tombstones(records)
    path.write_text(json.dumps({"v": STATE_VERSION, "records": pruned}, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Path safety -- THE path-traversal gate for every relpath crossing the wire
# --------------------------------------------------------------------------

def _safe_relpath(rel: object) -> str | None:
    """Reject anything that isn't a plain, relative, POSIX-style path
    component list: not a string, empty, a NUL byte, a backslash (forbidden
    outright rather than merely normalized -- a manifest can cross between a
    Linux and a Windows device, backslash is an ordinary filename character
    on one and a separator on the other, and refusing it means neither side
    has to guess which the other one meant), or any "", "." or ".."
    component (which also catches a leading "/" -- splitting an absolute
    path on "/" always yields a leading empty component). Every relpath that
    reaches a filesystem call anywhere in this module -- a peer's manifest
    keys, a get_file/put_file/delete_file request's `path` -- MUST pass
    through here first; nothing downstream re-derives safety on its own.
    """
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel:
        return None
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return "/".join(parts)


# --------------------------------------------------------------------------
# Folder scan -- the mtime/size fingerprint pass (watch_folder.py's skip_cache
# idea, applied bidirectionally with tombstones for what disappeared)
# --------------------------------------------------------------------------

def scan_folder(devices_dir: Path, prev: dict) -> dict:
    """One fingerprint pass over `devices_dir`, returning this side's full
    current manifest -- not a delta: every relpath `prev` knew about that
    scan_folder doesn't have a reason to change is carried forward
    unmodified, so a caller can always pass this straight into `reconcile()`
    as one complete side.

    A file whose mtime_ns+size still match its last-recorded record is kept
    as-is without re-reading or re-hashing it -- watch_folder.publish_new_files'
    skip_cache idea, applied here to a full manifest instead of a one-way
    publish scan, which is what keeps calling this on a timer (net.py's
    `_device_watch_loop`) cheap regardless of how large the folder is. A
    changed fingerprint still isn't necessarily a real change -- e.g. a copy
    tool that preserves bytes but not mtimes -- so the content is re-hashed
    and, if it turns out identical to the previous record, that record's
    `updated_at` is kept too (its size/mtime_ns just catch up): otherwise
    every such touch would manufacture a fresh "edit" that could needlessly
    outrank a genuine edit made on the other device around the same time.

    `updated_at` is wall-clock time (time.time()), stamped fresh only the
    instant a REAL change is noticed -- never the file's own mtime, which a
    different machine's clock (or a copy tool) cannot be trusted to agree
    with, and which reconcile()'s entire "newest wins" model depends on
    being comparable across two different devices' manifests.

    A relpath present in `prev` that is simply gone from disk now becomes a
    tombstone (`deleted: True`) rather than vanishing from the returned
    manifest -- that tombstone is the only thing that makes a delete
    propagate to the other device instead of the file just silently
    reappearing next time reconcile() runs against a peer who still has it.
    An already-deleted `prev` entry (a settled tombstone) is carried forward
    unchanged -- rescanning must never manufacture a fresh delete out of one
    that already happened.
    """
    devices_dir = Path(devices_dir)
    manifest: dict = {}
    seen: set[str] = set()
    if devices_dir.is_dir():
        for path in sorted(devices_dir.rglob("*")):
            # Symlinks are skipped rather than followed: this is a private
            # folder a user drops files into themselves, and following a
            # symlink into, say, a parent directory risks both mirroring
            # content the user never actually put here and (for a
            # self-referential link) an unbounded rglob.
            if path.is_symlink() or not path.is_file():
                continue
            try:
                rel = path.relative_to(devices_dir).as_posix()
            except ValueError:
                continue
            if rel.split("/", 1)[0] == _VERSIONS_DIR_NAME:
                continue
            safe = _safe_relpath(rel)
            if safe is None:
                continue  # can't happen for a real relative_to() result, but never trust blindly
            seen.add(safe)

            st = path.stat()
            prev_rec = prev.get(safe)
            if (isinstance(prev_rec, dict) and not prev_rec.get("deleted")
                    and prev_rec.get("size") == st.st_size and prev_rec.get("mtime_ns") == st.st_mtime_ns):
                manifest[safe] = prev_rec
                continue

            sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            if isinstance(prev_rec, dict) and not prev_rec.get("deleted") and prev_rec.get("sha256") == sha256:
                manifest[safe] = {**prev_rec, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
                continue

            manifest[safe] = {
                "sha256": sha256, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                "deleted": False, "updated_at": time.time(),
            }

    for relpath, rec in prev.items():
        if relpath in seen or not isinstance(rec, dict):
            continue
        if rec.get("deleted"):
            manifest[relpath] = rec  # an already-settled tombstone -- unchanged
        else:
            manifest[relpath] = {
                "sha256": None, "size": 0, "mtime_ns": 0,
                "deleted": True, "updated_at": time.time(),
            }
    return manifest


# --------------------------------------------------------------------------
# Reconcile -- pure, offline, the whole "who wins" decision
# --------------------------------------------------------------------------

@dataclass
class Action:
    op: str        # "pull" (fetch/adopt `record`'s content) | "delete" (adopt `record`'s tombstone)
    relpath: str
    record: dict   # the winning record this side must end up matching


def reconcile(local: dict, remote: dict) -> tuple[list[Action], list[Action]]:
    """Decide, per relpath, which side (if either) is behind and what it
    must do to catch up. Pure and total: no I/O, no clock reads, nothing
    but the two manifests handed in -- exactly what makes this half of the
    engine unit-testable with plain dicts.

    Rule, per relpath: whichever record has the higher `updated_at` wins; a
    side with **no** record for a relpath at all counts as trivially behind
    whatever the other side has (there's nothing to compare against). If the
    winner is a live file and the loser's record doesn't already match it
    (missing, tombstoned, or a different sha256), the loser gets a "pull"
    action for the winning record. If the winner is a tombstone and the
    loser currently has a live file, the loser gets a "delete" action. An
    exact `updated_at` tie, or a loser that already matches the winner
    (same sha256, or both tombstoned), produces no action at all -- "equal
    sha -> nothing" is what stops a sync from re-transferring bytes that are
    already identical on both ends just because their timestamps differ
    (e.g. after a plain copy that changed mtime but not content).

    Returns (actions_for_local, actions_for_remote): what THIS side must do
    to itself, and what the OTHER side must do (which reconcile_with_device,
    the network client, carries out on its behalf via put_file/delete_file --
    reconcile() itself never touches a socket or a filesystem).
    """
    local_actions: list[Action] = []
    remote_actions: list[Action] = []

    for relpath in sorted(set(local) | set(remote)):
        local_rec = local.get(relpath)
        remote_rec = remote.get(relpath)

        if local_rec is None:
            winner, loser_side, loser_rec = remote_rec, "local", None
        elif remote_rec is None:
            winner, loser_side, loser_rec = local_rec, "remote", None
        elif local_rec.get("updated_at", 0) > remote_rec.get("updated_at", 0):
            winner, loser_side, loser_rec = local_rec, "remote", remote_rec
        elif remote_rec.get("updated_at", 0) > local_rec.get("updated_at", 0):
            winner, loser_side, loser_rec = remote_rec, "local", local_rec
        else:
            continue  # exact tie -- neither side is "behind"

        target = local_actions if loser_side == "local" else remote_actions

        if winner.get("deleted"):
            if loser_rec is None or not loser_rec.get("deleted"):
                target.append(Action("delete", relpath, winner))
            continue

        if loser_rec is None or loser_rec.get("deleted") or loser_rec.get("sha256") != winner.get("sha256"):
            target.append(Action("pull", relpath, winner))
        # else: loser already has the same bytes -- nothing to do

    return local_actions, remote_actions


# --------------------------------------------------------------------------
# Server: SYNC_ALPN -- trusted-only, per-request framing shared with net.py
# --------------------------------------------------------------------------

def _build_sync_response(request: dict, devices_dir: Path, state_path: Path) -> dict:
    """Pure per-request handler: given one already-parsed JSON request, do
    whatever it asks against `devices_dir`/`state_path` and return the
    response dict -- no network here at all, which is what makes "server
    rejects a traversing path" directly unit-testable (tests/test_device_sync.py)
    without a live connection. handle_sync_connection below is the only
    thing that ever calls this from a real socket.

    Every `path` is re-validated with `_safe_relpath` here, even though
    reconcile_with_device (the trusted client this project ships) already
    filters the manifest it reconciles against -- this is the boundary that
    actually matters: a compromised or buggy paired device is still just
    "trusted", not "infallible", and this is the last gate before a request
    ever touches a filesystem call.
    """
    op = request.get("op")

    if op == "manifest":
        manifest = scan_folder(devices_dir, load_state(state_path))
        save_state(manifest, state_path)
        return {"records": manifest}

    if op == "get_file":
        rel = _safe_relpath(request.get("path"))
        if rel is None:
            return {"error": "invalid path"}
        file_path = Path(devices_dir) / rel
        if not file_path.is_file():
            return {"error": "not found"}
        data = file_path.read_bytes()
        if len(data) > net.MAX_MESSAGE_BYTES:
            # Base64 inflates by ~4/3; anything even close to the cap here
            # would fail unreadably on the other end's _recv_message anyway
            # -- refused with a clear reason instead.
            return {"error": "file too large to sync"}
        return {"content_base64": base64.b64encode(data).decode("ascii")}

    if op == "put_file":
        rel = _safe_relpath(request.get("path"))
        if rel is None:
            return {"error": "invalid path"}
        record = request.get("record")
        if not isinstance(record, dict):
            return {"error": "missing record"}
        try:
            content = base64.b64decode(request.get("content_base64") or "", validate=False)
        except (ValueError, TypeError):
            return {"error": "invalid content"}
        file_path = Path(devices_dir) / rel
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        state = load_state(state_path)
        # The sha256/size actually written, not merely what the caller
        # claimed in `record` -- a basic integrity check that costs nothing
        # (the bytes are already in hand) and means a transport hiccup or a
        # buggy peer produces a clearly-wrong local record rather than a
        # silently-trusted one.
        state[rel] = {
            "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
            "mtime_ns": record.get("mtime_ns", 0), "deleted": False,
            "updated_at": record.get("updated_at", time.time()),
        }
        save_state(state, state_path)
        return {"ok": True}

    if op == "delete_file":
        rel = _safe_relpath(request.get("path"))
        if rel is None:
            return {"error": "invalid path"}
        record = request.get("record")
        if not isinstance(record, dict):
            return {"error": "missing record"}
        (Path(devices_dir) / rel).unlink(missing_ok=True)
        state = load_state(state_path)
        state[rel] = {
            "sha256": None, "size": 0, "mtime_ns": 0, "deleted": True,
            "updated_at": record.get("updated_at", time.time()),
        }
        save_state(state, state_path)
        return {"ok": True}

    return {"error": f"unknown op {op!r}"}


async def _handle_sync_request(bi, devices_dir: Path, state_path: Path) -> None:
    try:
        request = await net._recv_message(bi)
        response = _build_sync_response(request, devices_dir, state_path)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the caller, never a crashed handler
        response = {"error": str(exc)}
    try:
        await net._send_message(bi, response)
    except Exception:
        pass  # peer likely disconnected mid-response -- nothing more to do


async def handle_sync_connection(conn, devices_dir: Path, state_path: Path) -> None:
    """The SYNC_ALPN branch of net._handle_connection (wired in net.serve()).

    Takes an ALREADY-ACCEPTED, already-connected `conn` -- net._handle_connection
    has to accept and connect first to even learn `conn.alpn()` and decide to
    route here at all, so redoing that dance would be pointless. Authorizes
    by IDENTITY, not by request content: `conn.remote_id()` is the
    QUIC-authenticated pubkey (net.py's own docstring), so checking it once,
    before a single request is answered, is what makes this the private
    folder's whole access boundary. An untrusted key gets the connection
    closed with no response at all, the same "refuse, don't explain" posture
    a locked door takes rather than a "wrong password" prompt.
    """
    if not devices.is_trusted(str(conn.remote_id())):
        try:
            conn.close(0, b"")
        except Exception:
            pass
        return
    while True:
        try:
            bi = await conn.accept_bi()
        except Exception:
            return  # connection closed
        asyncio.create_task(_handle_sync_request(bi, devices_dir, state_path))


# --------------------------------------------------------------------------
# Client: reconcile_with_device -- dial, exchange manifests, apply both sides
# --------------------------------------------------------------------------

@dataclass
class SyncReport:
    peer_pubkey_hex: str
    pulled: int   # files (or deletes) applied to THIS side
    pushed: int   # files (or deletes) applied to the OTHER side


async def reconcile_with_device(
    ticket_str: str, identity: Identity, devices_dir: Path, state_path: Path, *, relay: bool = True,
) -> SyncReport:
    """Dial one paired device over SYNC_ALPN and converge both folders.

    Uses the same ticket->identity dial fallback net.sync_with_peer relies
    on (net.dial_with_fallback) so a device whose ticket has gone stale
    since it was paired is still reachable by identity alone, exactly like
    a public-feed peer is.
    """
    ep = await net.bind_endpoint(identity, relay=relay)
    try:
        conn = await net.dial_with_fallback(ep, ticket_str, SYNC_ALPN)
        peer_pubkey_hex = str(conn.remote_id())
        if not devices.is_trusted(peer_pubkey_hex):
            raise RuntimeError(
                f"{peer_pubkey_hex[:16]}... is not a paired device -- pair with it first "
                "(`roastmesh device pair`)"
            )

        local_manifest = scan_folder(devices_dir, load_state(state_path))
        save_state(local_manifest, state_path)

        manifest_response = await net._request(conn, {"op": "manifest"})
        if "error" in manifest_response:
            raise RuntimeError(
                f"device {peer_pubkey_hex[:16]}... returned an error for manifest: {manifest_response['error']}"
            )
        # Re-validated here even though the responding device is a trusted
        # pairing -- see _build_sync_response's own docstring on why
        # "trusted" never means "assumed infallible". Anything malformed is
        # just dropped rather than aborting the whole sync over one bad key.
        remote_manifest: dict = {}
        for rel, rec in (manifest_response.get("records") or {}).items():
            safe = _safe_relpath(rel)
            if safe is not None and isinstance(rec, dict):
                remote_manifest[safe] = rec

        local_actions, remote_actions = reconcile(local_manifest, remote_manifest)

        pulled = 0
        for action in local_actions:
            target = Path(devices_dir) / action.relpath
            if action.op == "pull":
                file_response = await net._request(conn, {"op": "get_file", "path": action.relpath})
                if "error" in file_response:
                    continue
                try:
                    content = base64.b64decode(file_response.get("content_base64") or "")
                except (ValueError, TypeError):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            else:
                target.unlink(missing_ok=True)
            local_manifest[action.relpath] = action.record
            pulled += 1

        pushed = 0
        for action in remote_actions:
            if action.op == "pull":
                source = Path(devices_dir) / action.relpath
                try:
                    content = source.read_bytes()
                except OSError:
                    continue
                response = await net._request(conn, {
                    "op": "put_file", "path": action.relpath,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                    "record": action.record,
                })
            else:
                response = await net._request(
                    conn, {"op": "delete_file", "path": action.relpath, "record": action.record})
            if "error" not in response:
                pushed += 1

        save_state(local_manifest, state_path)
        return SyncReport(peer_pubkey_hex=peer_pubkey_hex, pulled=pulled, pushed=pushed)
    finally:
        await ep.close()


# --------------------------------------------------------------------------
# Pairing over LAN: discover, connect, run the SAS handshake, trust on success
# --------------------------------------------------------------------------

def _pairing_wire(conn) -> tuple[Callable[[dict], Awaitable[None]], Callable[[], Awaitable[dict]]]:
    """Adapt a live QUIC connection into the generic async send()/recv()
    pairing.run_pairing expects. Each logical message gets its OWN fresh
    bidirectional stream -- net._send_message/_recv_message's framing is
    exactly one message per stream (write_all + finish, then read_to_end),
    which is the one thing it cannot do is carry several messages on one
    stream. Order-independent by construction: `send` doesn't wait for a
    reply on the stream it just wrote, and `recv` just waits for the next
    stream the peer opens -- see pairing.run_pairing's own docstring on why
    that symmetry, not `is_initiator`, is what the protocol actually relies
    on."""
    async def send(msg: dict) -> None:
        bi = await conn.open_bi()
        await net._send_message(bi, msg)

    async def recv() -> dict:
        bi = await conn.accept_bi()
        return await net._recv_message(bi)

    return send, recv


async def _exchange_device_info(conn, our_name: str) -> tuple[str, str]:
    """After a successful SAS confirmation, trade display name + platform so
    each side's devices.json entry for the other is more than a bare pubkey.
    Deliberately separate from pairing.run_pairing's own wire messages (a
    device's advertised name is cosmetic, unauthenticated small talk, not
    part of what the SAS/signature proves) -- reuses the same `_pairing_wire`
    framing rather than inventing a second one."""
    send, recv = _pairing_wire(conn)
    await send({"name": our_name, "platform": sys.platform})
    peer_info = await recv()
    name = peer_info.get("name") if isinstance(peer_info, dict) else None
    platform = peer_info.get("platform") if isinstance(peer_info, dict) else None
    return (str(name).strip() or "roastmesh device") if name else "roastmesh device", \
        str(platform) if platform else "unknown"


def _clean_hostname(name: str | None) -> str:
    candidate = (name or "").strip()
    if candidate:
        return candidate
    try:
        return socket.gethostname().strip() or "roastmesh device"
    except OSError:
        return "roastmesh device"


OnStatus = Callable[[list[PairingCandidate]], "PairingCandidate | None | Awaitable[PairingCandidate | None]"]


async def pair_over_lan(
    identity: Identity,
    *,
    confirm: Callable[[list], object],
    timeout: float = 60.0,
    on_status: OnStatus | None = None,
    name: str | None = None,
) -> PairResult:
    """Find another device in pairing mode on the LAN, run the SAS
    handshake with it, and -- only on success -- add it to this identity's
    trusted-device set.

    Discovery (lan_discovery.discover_pairing_beacons) broadcasts our own
    pairing hello and collects everyone else's. Exactly one candidate is
    auto-selected; anything else (zero, or more than one) is handed to
    `on_status(candidates)` for the caller to resolve -- called with the
    full candidate list either way once discovery settles, so a caller (the
    CLI's `--json` mode) can always report what was found, but its return
    value only overrides the automatic single-candidate choice.

    Which side dials and which side accepts is decided the same way on both
    ends without any coordination beyond the two pubkeys themselves: the
    lexicographically lower public key dials, the higher one accepts. This
    is a plain endpoint of its own for the duration of pairing -- not
    net.serve()'s already-running node, even if one is up on this same
    identity -- for the same reason sync_with_peer already binds its own
    endpoint for a manual sync: a short-lived, one-off exchange has no
    business being entangled with whatever the long-running server is doing.
    A `node serve` PAIR_ALPN dial still lands somewhere sane (see net.py's
    `_handle_connection`), it just isn't how this function itself pairs.

    On success, `devices.add_device` is called with the other side's pubkey,
    its advertised name/platform (traded after the SAS confirms -- see
    `_exchange_device_info`), and now as `paired_at`. The peer is expected
    to do the exact same thing on its own end; this function only ever
    writes to *this* identity's own devices.json.
    """
    hostname = _clean_hostname(name)
    code = f"{secrets.randbelow(10000):04d}"
    own_pubkey_hex = identity.public_key_hex
    deadline = time.monotonic() + timeout

    ep = await net.bind_endpoint(identity, alpns=[PAIR_ALPN], relay=True)
    try:
        ticket = str(iroh.EndpointTicket.from_addr(ep.addr()))

        discover_budget = max(1.0, min(10.0, deadline - time.monotonic()))
        candidates = await discover_pairing_beacons(
            own_pubkey_hex, ticket, code=code, hostname=hostname, listen_s=discover_budget,
        )

        picked = None
        if on_status is not None:
            picked = on_status(candidates)
            if hasattr(picked, "__await__"):
                picked = await picked

        if picked is not None:
            chosen = picked
        elif len(candidates) == 1:
            chosen = candidates[0]
        elif not candidates:
            return PairResult(False, None, None, error="no other device found nearby in pairing mode")
        else:
            return PairResult(
                False, None, None,
                error=f"found {len(candidates)} devices in pairing mode at once -- pick one",
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return PairResult(False, None, None, error="timed out before pairing could complete")

        is_initiator = own_pubkey_hex < chosen.pubkey
        try:
            if is_initiator:
                conn = await net.dial_with_fallback(ep, chosen.ticket, PAIR_ALPN, timeout=remaining)
            else:
                incoming = await asyncio.wait_for(ep.accept_next(), remaining)
                if incoming is None:
                    return PairResult(False, None, None, error="connection closed before pairing completed")
                accepting = await incoming.accept()
                conn = await accepting.connect()
        except Exception as exc:  # noqa: BLE001 -- a failed dial/accept is an ordinary outcome
            # (the candidate went offline between being seen and being
            # dialled, a malformed ticket, a timed-out accept, ...), never
            # an unhandled crash reaching the CLI/GUI caller.
            return PairResult(False, None, None, error=f"could not connect: {exc!r}")

        remote_pubkey_hex = str(conn.remote_id())
        if remote_pubkey_hex != chosen.pubkey:
            # Connected, but to a different identity than the beacon claimed
            # -- exactly the beacon-substitution attack SAS exists to catch
            # (this module's own docstring), caught here before a human is
            # even shown an emoji.
            return PairResult(False, None, None, error="connected to an unexpected identity")

        send, recv = _pairing_wire(conn)
        try:
            result = await run_pairing(
                send=send, recv=recv, own_identity=identity, remote_pubkey_hex=remote_pubkey_hex,
                is_initiator=is_initiator, confirm=confirm,
            )
        except Exception as exc:  # noqa: BLE001 -- e.g. the connection dropping mid-handshake
            return PairResult(False, None, None, error=f"pairing failed: {exc!r}")

        if result.ok:
            try:
                peer_name, peer_platform = await asyncio.wait_for(
                    _exchange_device_info(conn, hostname), max(1.0, deadline - time.monotonic()))
            except Exception:  # noqa: BLE001 -- a name is cosmetic; the SAS trust decision already stands
                peer_name, peer_platform = "roastmesh device", "unknown"
            devices.add_device(Device(
                pubkey=result.remote_pubkey_hex, name=peer_name, platform=peer_platform,
                paired_at=datetime.now(timezone.utc).isoformat(),
            ))
        return result
    finally:
        await ep.close()
