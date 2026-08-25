from __future__ import annotations

from pathlib import Path

from roastnet.gui.config import GuiConfig, load_config, save_config


def _isolate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("roastnet.gui.config.config_path", lambda: tmp_path / "gui_config.json")


def test_load_config_returns_defaults_when_no_file_exists(monkeypatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    cfg = load_config()
    assert cfg.db_path
    assert cfg.watch_dir
    assert cfg.wan_discovery_enabled is False


def test_save_then_load_roundtrips(monkeypatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    save_config(GuiConfig(db_path="/x/db.sqlite3", watch_dir="/x/watch", wan_discovery_enabled=True))
    cfg = load_config()
    assert cfg == GuiConfig(db_path="/x/db.sqlite3", watch_dir="/x/watch", wan_discovery_enabled=True)


def test_load_config_falls_back_to_defaults_on_corrupt_file(monkeypatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "gui_config.json").write_text("not valid json{{{")
    cfg = load_config()
    assert cfg.db_path
    assert cfg.watch_dir
