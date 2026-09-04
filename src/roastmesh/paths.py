"""Where roastmesh keeps its files, with a fallback to the old name.

The project was called roastnet until v0.5.0, and existing installs have their
data under `~/.local/share/roastnet` and `~/.config/roastnet`. That data is not
regenerable: the identity file *is* the user's cryptographic identity, and the
feed is the signed, hash-chained history other peers have already replicated.
Pointing a renamed build at fresh directories would hand every existing user a
new public key, silently orphan everything they had published, and make their
peers stop recognising them.

So: use the new location, unless the old one exists and the new one does not --
in which case keep using the old one, exactly where it already is. Nothing is
moved, copied, or deleted, because a migration that runs on someone's only copy
of an unrecoverable key is a worse risk than an inconsistent directory name.
A fresh install has no legacy directory and simply gets the new paths.

Every function computes its answer on each call rather than caching it at
import, which is what lets tests point HOME somewhere isolated -- the same
convention identity.py's docstring already relied on.
"""
from __future__ import annotations

from pathlib import Path

APP_NAME = "roastmesh"
LEGACY_APP_NAME = "roastnet"


def _prefer_existing(new: Path, legacy: Path) -> Path:
    """`new`, unless only `legacy` exists -- then keep using what is there."""
    if not new.exists() and legacy.exists():
        return legacy
    return new


def data_dir() -> Path:
    """Feed, peer feeds, peers.json, the search index, the DHT node cache."""
    home = Path.home()
    return _prefer_existing(home / ".local" / "share" / APP_NAME,
                            home / ".local" / "share" / LEGACY_APP_NAME)


def config_dir() -> Path:
    """The identity file. Separate from data_dir by XDG convention."""
    home = Path.home()
    return _prefer_existing(home / ".config" / APP_NAME,
                            home / ".config" / LEGACY_APP_NAME)


def default_watch_dir() -> Path:
    """The drop folder whose .alog files are published automatically.

    Same fallback: a user with roasts already sitting in ~/RoastNetShare should
    keep publishing from it rather than silently start watching an empty new
    folder while their files sit unshared.
    """
    home = Path.home()
    return _prefer_existing(home / "RoastMeshShare", home / "RoastNetShare")


def default_devices_dir() -> Path:
    """The drop folder that mirrors between a user's own paired devices.

    Deliberately a *different* folder from default_watch_dir: that one is a
    one-way broadcast to the whole public feed, this one is a private,
    bidirectional mirror between only your own SAS-verified devices (see
    device_sync.py) -- conflating them would mean a file dropped for one
    audience quietly reaching the other. Same legacy-fallback convention as
    every other visible folder here, on the off chance someone already has a
    "RoastNetDevices" from a pre-release build of this feature under the old
    project name.
    """
    home = Path.home()
    return _prefer_existing(home / "RoastMeshDevices", home / "RoastNetDevices")


def device_sync_state_path() -> Path:
    """Internal bookkeeping for the folder mirror: the per-relpath manifest
    (content hash, size, mtime, tombstone) device_sync.py reconciles against a
    paired device's own manifest. Lives under data_dir() alongside peers.json
    and the feed -- it's local state, not something the user edits by hand --
    and, like every path here, is computed fresh per call rather than cached,
    so tests that monkeypatch HOME never see a stale value baked in at import
    time (see this module's own docstring)."""
    return data_dir() / "device_sync_state.json"
