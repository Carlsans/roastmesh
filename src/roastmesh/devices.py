"""The trusted-device set: which other Ed25519 identities are "also you".

Distinct from peers.py on purpose. A peer is anyone whose public feed you've
synced with -- no trust implied beyond "their signatures verify". A device is
the opposite: a much smaller, much higher-trust set, written only after a
human compared 7 emoji on two screens and confirmed they matched
(pairing.py's SAS handshake). Membership here is what net.py's device-sync
connection handler checks before touching disk for a peer at all (see
device_sync.py) -- so this file is, in effect, the private folder mirror's
entire access-control list.

Storage is a plain JSON list, same shape and same reasoning as peers.py's
peers.json: small, human-inspectable, no need for a SQLite table. Kept under
config_dir() (identity.json's directory) rather than data_dir() (peers.json's)
-- devices.json is closer in kind to "who am I" than "what have I seen", and
losing it only means re-pairing, not losing any replicated data.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from roastmesh.paths import config_dir

# Same shape as feed._PUBKEY_RE / hello._PUBKEY_RE: a device's pubkey becomes
# part of the trust check net.py performs against conn.remote_id() before
# ever touching disk on its behalf, so it is validated the same strictly as
# any value that guards a filesystem or authorization decision elsewhere in
# this project.
_PUBKEY_RE = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass
class Device:
    pubkey: str      # 64-hex Ed25519 -- the other device's own feed identity
    name: str        # human label, e.g. "Carl's Pi" -- cosmetic only
    platform: str    # "linux" | "win32" | "darwin", whatever sys.platform said
    paired_at: str   # ISO-8601 UTC, when the SAS handshake succeeded


def default_devices_path() -> Path:
    """Computed fresh on every call, never cached -- the same convention
    identity.default_identity_path documents: a frozen constant would make a
    test that monkeypatches HOME silently write to the real user's config."""
    return config_dir() / "devices.json"


def device_from_dict(d: dict) -> Device:
    """Build a `Device` from an untrusted dict -- one loaded from disk, or
    one carried over the wire during pairing (pairing.py exchanges name and
    platform after the SAS confirms). Filters to this version's own known
    fields first, the same unknown-key-tolerant pattern peers.peer_from_dict
    uses, so a field a newer version adds someday doesn't blow up an older
    one's whole devices.json load or an in-progress pairing over one extra,
    harmless key."""
    known = {f.name for f in fields(Device)}
    return Device(**{k: v for k, v in d.items() if k in known})


def load_devices(path: Path | None = None) -> list[Device]:
    path = path or default_devices_path()
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    devices = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        pubkey = d.get("pubkey")
        if not isinstance(pubkey, str) or not _PUBKEY_RE.match(pubkey):
            # A malformed entry here isn't just bad data -- if it ever leaked
            # through to the device-sync trust check, is_trusted() would be
            # comparing conn.remote_id() against garbage instead of a real
            # pubkey. Dropped silently, the same posture load_peers takes
            # toward entries peer_from_dict can't make sense of, rather than
            # raising and taking the whole load down over one bad row.
            continue
        devices.append(device_from_dict(d))
    return devices


def save_devices(devices: list[Device], path: Path | None = None) -> None:
    path = path or default_devices_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(d) for d in devices], indent=2), encoding="utf-8")
    # 0o600 like identity.save_identity -- this file names exactly which
    # other keys on the network this machine will accept a private-folder
    # sync connection from, which is not something another local user on a
    # shared machine has any business reading or, worse, editing.
    os.chmod(path, 0o600)


def add_device(dev: Device, path: Path | None = None) -> None:
    """Upsert by pubkey -- re-pairing the same device (a new name, or just
    running `pair` again) replaces its row rather than accumulating a
    duplicate that would otherwise still pass is_trusted() but confuse
    `device list`."""
    if not _PUBKEY_RE.match(dev.pubkey):
        raise ValueError(f"refusing to trust a non-pubkey device id: {dev.pubkey!r}")
    devices = [d for d in load_devices(path) if d.pubkey != dev.pubkey]
    devices.append(dev)
    save_devices(devices, path)


def remove_device(pubkey: str, path: Path | None = None) -> bool:
    """Returns whether a device with that pubkey existed to remove. Never
    touches the synced folder itself -- untrusting a device stops future
    syncs, it does not retroactively undo files it already wrote here (the
    same "revoking access doesn't erase what was already shared" posture
    peers.py/feed.py take toward the public feed)."""
    devices = load_devices(path)
    kept = [d for d in devices if d.pubkey != pubkey]
    if len(kept) == len(devices):
        return False
    save_devices(kept, path)
    return True


def is_trusted(pubkey: str, path: Path | None = None) -> bool:
    """The device-sync authorization check: does `pubkey` (conn.remote_id()
    on an incoming SYNC_ALPN connection -- net.py) belong to a device this
    one has paired with. Deliberately case-sensitive/exact -- _PUBKEY_RE
    already normalizes what can ever be stored, so there is no case-folding
    ambiguity to worry about here."""
    return any(d.pubkey == pubkey for d in load_devices(path))
