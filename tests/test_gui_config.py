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
    assert cfg.wan_discovery_enabled is True
    assert cfg.language == "en"


def test_save_then_load_roundtrips(monkeypatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    save_config(GuiConfig(db_path="/x/db.sqlite3", watch_dir="/x/watch", wan_discovery_enabled=True,
                           language="fr"))
    cfg = load_config()
    assert cfg == GuiConfig(db_path="/x/db.sqlite3", watch_dir="/x/watch", wan_discovery_enabled=True,
                             language="fr")


def test_load_config_falls_back_to_defaults_on_corrupt_file(monkeypatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "gui_config.json").write_text("not valid json{{{")
    cfg = load_config()
    assert cfg.db_path
    assert cfg.watch_dir


def test_load_config_rejects_an_unknown_language(monkeypatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "gui_config.json").write_text('{"language": "xx"}')
    cfg = load_config()
    assert cfg.language == "en"


def test_load_config_accepts_a_partial_file_with_only_language(monkeypatch, tmp_path: Path) -> None:
    # This is the exact shape install.sh writes -- every other field must
    # still come out as a sensible default, not a crash or a blank value.
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "gui_config.json").write_text('{"language": "fr"}')
    cfg = load_config()
    assert cfg.language == "fr"
    assert cfg.db_path
    assert cfg.watch_dir
    assert cfg.wan_discovery_enabled is True
    assert cfg.temp_unit == "C"
    assert cfg.ui_scale is None


def test_load_config_defaults_ui_scale_to_none_when_no_file_exists(monkeypatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    assert load_config().ui_scale is None


def test_save_then_load_roundtrips_a_ui_scale_override(monkeypatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    save_config(GuiConfig(db_path="/x/db.sqlite3", watch_dir="/x/watch", ui_scale=2.0))
    assert load_config().ui_scale == 2.0


def test_load_config_rejects_a_ui_scale_outside_the_valid_range(monkeypatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "gui_config.json").write_text('{"ui_scale": 999}')
    assert load_config().ui_scale is None


def test_load_config_rejects_a_non_numeric_ui_scale(monkeypatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "gui_config.json").write_text('{"ui_scale": "big"}')
    assert load_config().ui_scale is None
