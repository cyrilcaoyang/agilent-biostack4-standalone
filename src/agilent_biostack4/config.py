"""TOML config loader for the BioStack 4 driver.

Pattern lifted from ``agilent_plateloc.config``: look for ``config.toml``
walking up from this file, fall back to ``CWD/config.toml``, and serve
defaults if no file is present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


_DEFAULTS: dict[str, Any] = {
    "instrument": {
        "com_port": "COM8",
        "baudrate": 9600,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 2,
        "ack_timeout": 1.0,
        "header_timeout": 30.0,
        "payload_timeout": 5.0,
    },
    "service": {
        "dry_run": False,
    },
    "dashboard": {
        "equipment_id": "agilent_biostack",
        "equipment_name": "Agilent BioStack 4",
    },
}


def _find_config_file() -> Path | None:
    here = Path(__file__).resolve().parent
    for parent in (here, here.parent, here.parent.parent, here.parent.parent.parent):
        candidate = parent / "config.toml"
        if candidate.is_file():
            return candidate
    cwd = Path.cwd() / "config.toml"
    if cwd.is_file():
        return cwd
    return None


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load ``config.toml`` as a nested dict, or return the built-in defaults."""
    if tomllib is None:
        return _DEFAULTS
    if path is None:
        found = _find_config_file()
        if found is None:
            return _DEFAULTS
        path = found
    with open(path, "rb") as f:
        loaded = tomllib.load(f)
    merged = {section: dict(values) for section, values in _DEFAULTS.items()}
    for section, values in loaded.items():
        merged.setdefault(section, {}).update(values)
    return merged


def get(cfg: dict[str, Any], section: str, key: str, default: Any = None) -> Any:
    """Read ``cfg[section][key]`` with a fallback through the defaults."""
    if section in cfg and key in cfg[section]:
        return cfg[section][key]
    return _DEFAULTS.get(section, {}).get(key, default)
