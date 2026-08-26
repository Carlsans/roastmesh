"""Persisted GUI preferences -- database file, publish watch folder,
whether internet-wide discovery is on -- so a choice made in the Settings
tab survives closing and reopening the app instead of resetting to
defaults every launch.

CLI-only usage is untouched by this: the CLI takes explicit flags/defaults
of its own (cli.py's DEFAULT_DB, feed.py's default_feed_dir, etc.) and
never reads this file. This is GUI state, not project config.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from roastnet.gui.i18n import DEFAULT_LANGUAGE, LANGUAGES
from roastnet.index.db import default_db_path
from roastnet.watch_folder import default_watch_dir


def config_path() -> Path:
    return Path.home() / ".local" / "share" / "roastnet" / "gui_config.json"


@dataclass
class GuiConfig:
    db_path: str
    watch_dir: str
    wan_discovery_enabled: bool = False
    temp_unit: str = "C"  # "C" or "F" -- see gui/units.py; display-only, never affects stored data
    language: str = DEFAULT_LANGUAGE  # see gui/i18n.py; a stale/unknown value falls back, never crashes


def default_config() -> GuiConfig:
    return GuiConfig(
        db_path=str(default_db_path()),
        watch_dir=str(default_watch_dir()),
        wan_discovery_enabled=False,
        temp_unit="C",
        language=DEFAULT_LANGUAGE,
    )


def load_config() -> GuiConfig:
    path = config_path()
    defaults = default_config()
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return defaults
    return GuiConfig(
        db_path=data.get("db_path") or defaults.db_path,
        watch_dir=data.get("watch_dir") or defaults.watch_dir,
        wan_discovery_enabled=bool(data.get("wan_discovery_enabled", False)),
        temp_unit=data.get("temp_unit") if data.get("temp_unit") in ("C", "F") else defaults.temp_unit,
        language=data.get("language") if data.get("language") in LANGUAGES else defaults.language,
    )


def save_config(cfg: GuiConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2))
