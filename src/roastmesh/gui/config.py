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

from roastmesh.gui.i18n import DEFAULT_LANGUAGE, LANGUAGES
from roastmesh.gui.widgets import MAX_UI_SCALE, MIN_UI_SCALE
from roastmesh.index.db import default_db_path
from roastmesh.watch_folder import default_watch_dir
from roastmesh.paths import data_dir


def config_path() -> Path:
    return data_dir() / "gui_config.json"


@dataclass
class GuiConfig:
    db_path: str
    watch_dir: str
    wan_discovery_enabled: bool = True
    temp_unit: str = "C"  # "C" or "F" -- see gui/units.py; display-only, never affects stored data
    language: str = DEFAULT_LANGUAGE  # see gui/i18n.py; a stale/unknown value falls back, never crashes
    # None means "detect from this screen's resolution" (gui/widgets.py's
    # detect_ui_scale) -- set to a number via Ctrl+scroll/Ctrl+plus/minus
    # in the GUI, at which point it sticks across restarts and screens
    # until reset (Ctrl+0).
    ui_scale: float | None = None
    # "" means none, "auto" means ask the router (PCP/NAT-PMP), a number means
    # that port. The port other machines can reach this one on, when a router
    # or VPN forwards one here -- see wan_discovery.needs_public_port for when
    # it is required. Kept as text because it comes straight from an entry box,
    # and both an empty box and the word "auto" have to survive a round trip.
    public_port: str = ""


def default_config() -> GuiConfig:
    return GuiConfig(
        db_path=str(default_db_path()),
        watch_dir=str(default_watch_dir()),
        wan_discovery_enabled=True,
        temp_unit="C",
        language=DEFAULT_LANGUAGE,
        ui_scale=None,
        public_port="",
    )


def load_config() -> GuiConfig:
    path = config_path()
    defaults = default_config()
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    return GuiConfig(
        db_path=data.get("db_path") or defaults.db_path,
        watch_dir=data.get("watch_dir") or defaults.watch_dir,
        wan_discovery_enabled=bool(data.get("wan_discovery_enabled", True)),
        temp_unit=data.get("temp_unit") if data.get("temp_unit") in ("C", "F") else defaults.temp_unit,
        language=data.get("language") if data.get("language") in LANGUAGES else defaults.language,
        ui_scale=_valid_ui_scale(data.get("ui_scale")),
        public_port=_valid_port(data.get("public_port")),
    )


def _valid_port(value: object) -> str:
    """A port, or "" -- never anything that would become a bad CLI argument.

    Whatever survives here is handed straight to `node serve --public-port`,
    so junk in the config file must not turn into a serve process that
    refuses to start; an unusable value is simply no value.
    """
    text = str(value).strip().lower()
    if text == "auto":
        return "auto"
    try:
        port = int(text)
    except (TypeError, ValueError):
        return ""
    return str(port) if 1 <= port <= 65535 else ""


def _valid_ui_scale(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value) if MIN_UI_SCALE <= value <= MAX_UI_SCALE else None


def save_config(cfg: GuiConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
