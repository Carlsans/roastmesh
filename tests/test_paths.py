"""paths.py: the roastnet -> roastmesh rename must not strand existing users.

Two things in this project are name-shaped but are actually compatibility
contracts, and both would be silently broken by a thorough find-and-replace:
the directories holding an unrecoverable identity and an already-replicated
feed, and the DHT info-hash that is the rendezvous point for the live network.
These tests exist so the next rename has to be deliberate about them.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from roastmesh import paths


def _fake_home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def test_fresh_install_uses_the_new_name(monkeypatch, tmp_path: Path) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    assert paths.data_dir() == home / ".local" / "share" / "roastmesh"
    assert paths.config_dir() == home / ".config" / "roastmesh"
    assert paths.default_watch_dir() == home / "RoastMeshShare"


def test_an_existing_roastnet_install_keeps_using_its_own_directories(monkeypatch, tmp_path: Path) -> None:
    """The case that matters: someone who installed before the rename.

    Their identity file cannot be regenerated and their feed is already
    replicated to peers under that key, so pointing them at empty new
    directories would hand them a new public key and orphan their published
    history. Nothing is moved -- the old location is simply used where it is.
    """
    home = _fake_home(monkeypatch, tmp_path)
    (home / ".local" / "share" / "roastnet").mkdir(parents=True)
    (home / ".config" / "roastnet").mkdir(parents=True)
    (home / "RoastNetShare").mkdir()

    assert paths.data_dir() == home / ".local" / "share" / "roastnet"
    assert paths.config_dir() == home / ".config" / "roastnet"
    assert paths.default_watch_dir() == home / "RoastNetShare"


def test_the_new_directory_wins_once_it_exists(monkeypatch, tmp_path: Path) -> None:
    """Legacy is a fallback, not a permanent preference: a user who has both
    (say, having started fresh on a new machine) follows the new name."""
    home = _fake_home(monkeypatch, tmp_path)
    (home / ".local" / "share" / "roastnet").mkdir(parents=True)
    (home / ".local" / "share" / "roastmesh").mkdir(parents=True)
    assert paths.data_dir() == home / ".local" / "share" / "roastmesh"


def test_the_real_modules_route_through_the_resolver(monkeypatch, tmp_path: Path) -> None:
    """The resolver is worthless if a call site still hardcodes a directory,
    so check the actual defaults every module hands out, not just paths.py."""
    home = _fake_home(monkeypatch, tmp_path)
    (home / ".local" / "share" / "roastnet").mkdir(parents=True)
    (home / ".config" / "roastnet").mkdir(parents=True)

    from roastmesh import feed, identity, peers, wan_discovery, watch_folder
    from roastmesh.gui import config as gui_config
    from roastmesh.index import db

    legacy = home / ".local" / "share" / "roastnet"
    assert feed.default_feed_dir() == legacy / "feed"
    assert feed.default_peer_feeds_root() == legacy / "peer_feeds"
    assert peers.default_peers_path() == legacy / "peers.json"
    assert db.default_db_path() == legacy / "index.sqlite3"
    assert gui_config.config_path() == legacy / "gui_config.json"
    assert wan_discovery.default_node_cache_path() == legacy / "dht_nodes.json"
    assert identity.default_identity_path() == home / ".config" / "roastnet" / "identity.json"
    # watch_folder has no legacy dir here, so it should take the new name
    assert watch_folder.default_watch_dir() == home / "RoastMeshShare"


def test_the_swarm_info_hash_did_not_change_with_the_rename() -> None:
    """The DHT rendezvous point is derived from the *old* project name, and has
    to stay that way.

    It is not a label -- every node looks itself up under it. Deriving it from
    "roastmesh" instead would put renamed nodes in a different neighbourhood of
    the DHT from everyone still running an older build, and the two halves
    would never find each other again. Pinned to the literal digest so a
    rename cannot quietly change it.
    """
    from roastmesh.wan_discovery import SWARM_INFO_HASH

    assert SWARM_INFO_HASH == hashlib.sha1(b"roastnet-swarm-v1").digest()
    assert SWARM_INFO_HASH.hex() == "229f37d1deca3f8181b32455b7d65a7b0cc58e5a"
