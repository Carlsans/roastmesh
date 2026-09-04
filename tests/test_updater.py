"""roastmesh.updater: version comparison, the GitHub check (mocked), install-kind
detection, and the `roastmesh update --check` CLI surface.

The actual download-and-swap path (perform_update) does real network + file I/O
and process spawning, so it is deliberately not exercised here; these cover the
decision logic that decides *whether* and *how* to update, which is where the
bugs would be."""
from __future__ import annotations

import json
import sys

import pytest
from click.testing import CliRunner

from roastmesh import updater
from roastmesh.cli import main


# -- version comparison -----------------------------------------------------

@pytest.mark.parametrize("latest,current,expected", [
    ("0.6.19", "0.6.18", True),
    ("0.6.18", "0.6.18", False),
    ("0.6.17", "0.6.18", False),
    ("0.7.0", "0.6.18", True),
    ("1.0.0", "0.9.9", True),
    ("0.6.18.1", "0.6.18", True),   # unequal lengths, longer-with-more is newer
    ("1.0", "0.9.9", True),
    ("v0.6.19", "0.6.18", True),    # leading v tolerated
])
def test_is_newer(latest: str, current: str, expected: bool) -> None:
    assert updater._is_newer(latest, current) is expected


def test_version_tuple_parses_and_tolerates_junk() -> None:
    assert updater._version_tuple("v0.6.18") == (0, 6, 18)
    assert updater._version_tuple("0.6.18-rc1") == (0, 6, 18)
    assert updater._version_tuple("garbage") == (0,)
    # garbage never looks newer than a real version
    assert updater._is_newer("garbage", "0.6.18") is False


# -- check_latest (GitHub API mocked) ---------------------------------------

class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False


def _mock_api(monkeypatch, *, payload: dict | None = None, exc: Exception | None = None) -> None:
    def fake(req, timeout=None):  # noqa: ANN001
        if exc is not None:
            raise exc
        return _FakeResp(json.dumps(payload).encode("utf-8"))
    monkeypatch.setattr(updater.urllib.request, "urlopen", fake)


def test_check_latest_reports_a_newer_release(monkeypatch) -> None:
    _mock_api(monkeypatch, payload={"tag_name": "v0.7.0", "html_url": "https://example/rel/0.7.0"})
    info = updater.check_latest(current="0.6.18")
    assert info is not None
    assert info.latest_version == "0.7.0"
    assert info.is_newer is True
    assert info.page_url == "https://example/rel/0.7.0"


def test_check_latest_not_newer_when_equal(monkeypatch) -> None:
    _mock_api(monkeypatch, payload={"tag_name": "v0.6.18"})
    info = updater.check_latest(current="0.6.18")
    assert info is not None and info.is_newer is False


def test_check_latest_not_newer_when_older(monkeypatch) -> None:
    _mock_api(monkeypatch, payload={"tag_name": "v0.6.17"})
    info = updater.check_latest(current="0.6.18")
    assert info is not None and info.is_newer is False


def test_check_latest_returns_none_on_network_error(monkeypatch) -> None:
    _mock_api(monkeypatch, exc=OSError("no network"))
    assert updater.check_latest(current="0.6.18") is None


def test_check_latest_returns_none_without_a_tag(monkeypatch) -> None:
    _mock_api(monkeypatch, payload={"html_url": "https://example"})
    assert updater.check_latest(current="0.6.18") is None


# -- asset_suffix -----------------------------------------------------------

@pytest.mark.parametrize("machine,expected", [
    ("x86_64", ""), ("AMD64", ""), ("amd64", ""),
    ("aarch64", "-aarch64"), ("arm64", "-aarch64"),
    ("riscv64", None), ("armv7l", None),
])
def test_asset_suffix(monkeypatch, machine: str, expected) -> None:
    monkeypatch.setattr(updater.platform, "machine", lambda: machine)
    assert updater.asset_suffix() == expected


# -- installation_kind ------------------------------------------------------

def test_installation_kind_unsupported_when_not_frozen(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert updater.installation_kind() == "unsupported"


def test_installation_kind_linux_binary(monkeypatch, tmp_path) -> None:
    (tmp_path / "roastmesh").write_text("x")
    (tmp_path / "roastmesh-gui").write_text("x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "roastmesh-gui"))
    monkeypatch.setattr(updater.platform, "machine", lambda: "x86_64")
    assert updater.installation_kind() == "linux-binary"


def test_installation_kind_unsupported_for_unknown_arch(monkeypatch, tmp_path) -> None:
    (tmp_path / "roastmesh").write_text("x")
    (tmp_path / "roastmesh-gui").write_text("x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "roastmesh-gui"))
    monkeypatch.setattr(updater.platform, "machine", lambda: "riscv64")
    assert updater.installation_kind() == "unsupported"


def test_installation_kind_windows_installer(monkeypatch, tmp_path) -> None:
    install = tmp_path / "Programs" / "roastmesh"
    install.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(install / "roastmesh-gui.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert updater.installation_kind() == "windows-installer"


def test_installation_kind_windows_portable_is_unsupported(monkeypatch, tmp_path) -> None:
    # A portable-zip build unpacked somewhere other than the installer's dir.
    elsewhere = tmp_path / "Downloads" / "roastmesh"
    elsewhere.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(elsewhere / "roastmesh-gui.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    assert updater.installation_kind() == "unsupported"


# -- CLI: update --check ----------------------------------------------------

def test_cli_update_check_json_reports_newer(monkeypatch) -> None:
    monkeypatch.setattr(
        updater, "check_latest",
        lambda current=None: updater.UpdateInfo("0.7.0", "https://example/rel", True))
    monkeypatch.setattr(updater, "is_supported", lambda: True)
    result = CliRunner().invoke(main, ["update", "--check", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip().splitlines()[-1])
    assert data["latest"] == "0.7.0"
    assert data["is_newer"] is True
    assert data["supported"] is True
    assert data["checked"] is True
    assert data["page_url"] == "https://example/rel"


def test_cli_update_check_json_when_offline(monkeypatch) -> None:
    monkeypatch.setattr(updater, "check_latest", lambda current=None: None)
    monkeypatch.setattr(updater, "is_supported", lambda: False)
    result = CliRunner().invoke(main, ["update", "--check", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip().splitlines()[-1])
    assert data["is_newer"] is False
    assert data["checked"] is False


def test_cli_update_refuses_when_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(
        updater, "check_latest",
        lambda current=None: updater.UpdateInfo("0.7.0", "https://example/rel", True))
    monkeypatch.setattr(updater, "is_supported", lambda: False)
    result = CliRunner().invoke(main, ["update", "--yes"])
    assert result.exit_code == 2
    assert "isn't supported" in result.output
    assert "https://example/rel" in result.output
