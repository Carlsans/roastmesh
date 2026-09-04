"""devices.py: the trusted-device set that net.py's device-sync connection
handler checks before touching disk on a peer's behalf -- in effect, the
private folder mirror's whole access-control list. Treated with the same
scrutiny as a security boundary because it is one.
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from roastmesh.devices import (
    Device,
    add_device,
    default_devices_path,
    device_from_dict,
    is_trusted,
    load_devices,
    remove_device,
    save_devices,
)

PUBKEY_A = "a" * 64
PUBKEY_B = "b" * 64


def _dev(pubkey: str, name: str = "Some Device") -> Device:
    return Device(pubkey=pubkey, name=name, platform="linux", paired_at="2026-01-01T00:00:00+00:00")


def test_default_devices_path_lives_under_config_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert default_devices_path() == tmp_path / ".config" / "roastmesh" / "devices.json"


def test_add_then_list_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    add_device(_dev(PUBKEY_A, "Carl's Pi"), path)
    devices = load_devices(path)
    assert len(devices) == 1
    assert devices[0].pubkey == PUBKEY_A
    assert devices[0].name == "Carl's Pi"


def test_add_device_upserts_by_pubkey_rather_than_duplicating(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    add_device(_dev(PUBKEY_A, "old name"), path)
    add_device(_dev(PUBKEY_A, "new name"), path)
    devices = load_devices(path)
    assert len(devices) == 1
    assert devices[0].name == "new name"


def test_remove_device_reports_whether_it_existed(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    add_device(_dev(PUBKEY_A), path)
    assert remove_device(PUBKEY_A, path) is True
    assert load_devices(path) == []
    assert remove_device(PUBKEY_A, path) is False


def test_remove_device_leaves_other_devices_untouched(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    add_device(_dev(PUBKEY_A, "A"), path)
    add_device(_dev(PUBKEY_B, "B"), path)
    remove_device(PUBKEY_A, path)
    remaining = load_devices(path)
    assert [d.pubkey for d in remaining] == [PUBKEY_B]


def test_is_trusted(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    assert is_trusted(PUBKEY_A, path) is False
    add_device(_dev(PUBKEY_A), path)
    assert is_trusted(PUBKEY_A, path) is True
    assert is_trusted(PUBKEY_B, path) is False


def test_add_device_rejects_a_malformed_pubkey(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    with pytest.raises(ValueError):
        add_device(_dev("not-a-pubkey"), path)
    assert load_devices(path) == []


def test_load_devices_drops_a_malformed_stored_entry_instead_of_raising(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    path.write_text(
        '[{"pubkey": "../../../etc/passwd", "name": "hostile", "platform": "linux", '
        '"paired_at": "2026-01-01T00:00:00+00:00"}, '
        '{"pubkey": "' + PUBKEY_A + '", "name": "ok", "platform": "linux", '
        '"paired_at": "2026-01-01T00:00:00+00:00"}]',
        encoding="utf-8",
    )
    devices = load_devices(path)
    assert [d.pubkey for d in devices] == [PUBKEY_A]


def test_device_from_dict_drops_unknown_keys() -> None:
    dev = device_from_dict({
        "pubkey": PUBKEY_A, "name": "N", "platform": "linux",
        "paired_at": "2026-01-01T00:00:00+00:00", "from_the_future": "ignored",
    })
    assert dev.pubkey == PUBKEY_A


def test_load_devices_with_no_file_returns_empty_list(tmp_path: Path) -> None:
    assert load_devices(tmp_path / "nope.json") == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits only")
def test_save_devices_writes_with_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    save_devices([_dev(PUBKEY_A)], path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
