"""Configuration loading for TableSense.

All settings live in ``config/config.yaml``; table polygons live in ``config/zones.json``.
Values are exposed through :class:`Config`, a thin attribute-access wrapper over dicts.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
# Override with env vars to run several restaurants/cameras from one install.
CONFIG_PATH = Path(os.getenv("TABLESENSE_CONFIG", CONFIG_DIR / "config.yaml"))
ZONES_PATH = Path(os.getenv("TABLESENSE_ZONES", CONFIG_DIR / "zones.json"))
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.getenv("TABLESENSE_DB", DATA_DIR / "tablesense.db"))
STATIC_DIR = ROOT / "static"


class Config(dict):
    """Dict with attribute access, recursively wrapping nested dicts."""

    def __getattr__(self, key: str) -> Any:
        try:
            value = self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc
        return Config(value) if isinstance(value, dict) and not isinstance(value, Config) else value

    def get_path(self, dotted: str, default: Any = None) -> Any:
        """Fetch a nested value such as ``"engine.seat_confirm_sec"``."""
        node: Any = self
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Load ``config.yaml``; raises a clear error if it is missing or invalid."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    log.info("Loaded config from %s (mode=%s)", path, data.get("mode"))
    return Config(data)


def save_config_values(updates: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    """Merge dotted-key ``updates`` into config.yaml (used by the setup page).

    Uses ruamel.yaml round-tripping so the user's comments and layout survive.
    """
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    def to_yaml(value: Any) -> Any:
        if isinstance(value, dict):
            m = CommentedMap((k, to_yaml(v)) for k, v in value.items())
            return m
        if isinstance(value, list):
            seq = CommentedSeq(to_yaml(v) for v in value)
            seq.fa.set_flow_style()  # keep [h, s, v] on one line
            return seq
        return value

    ry = YAML()
    ry.preserve_quotes = True
    with path.open("r", encoding="utf-8") as fh:
        data = ry.load(fh) or CommentedMap()
    for dotted, value in updates.items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = CommentedMap()
            node = node[part]
        node[parts[-1]] = to_yaml(value)
    tmp = path.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        ry.dump(data, fh)
    tmp.replace(path)  # atomic: never leave a half-written config


def load_zones(path: Path = ZONES_PATH) -> list[dict[str, Any]]:
    """Load table zone polygons drawn on the setup page (empty list if none yet)."""
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("tables", []) if isinstance(data, dict) else data
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read zones file %s: %s", path, exc)
        return []


def save_zones(zones: dict[str, Any], path: Path = ZONES_PATH) -> None:
    """Persist zone polygons to zones.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(zones, fh, indent=2)
