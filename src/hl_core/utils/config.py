"""Helper utilities for loading bot configuration files."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import importlib
import json
import tomllib


class ConfigError(RuntimeError):
    """Raised when a configuration file cannot be loaded."""


_SUPPORTED_TOML_EXTENSIONS: Final = {".toml"}
_SUPPORTED_YAML_EXTENSIONS: Final = {".yaml", ".yml"}
_SUPPORTED_JSON_EXTENSIONS: Final = {".json"}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:  # pragma: no cover - thin wrapper
        raise ConfigError(f"config file not found: {path}") from exc


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a configuration file supporting TOML, YAML, or JSON formats."""
    config_path = Path(path)
    suffix = config_path.suffix.lower()

    if suffix in _SUPPORTED_TOML_EXTENSIONS:
        text = _read_text(config_path)
        return tomllib.loads(text)

    if suffix in _SUPPORTED_JSON_EXTENSIONS:
        text = _read_text(config_path)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:  # pragma: no cover - pass-through
            raise ConfigError(f"invalid JSON config: {config_path}") from exc

    if suffix in _SUPPORTED_YAML_EXTENSIONS:
        text = _read_text(config_path)
        try:
            yaml = importlib.import_module("yaml")
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
            raise ConfigError("pyyaml is required to load YAML configs") from exc

        data = yaml.safe_load(text)  # type: ignore[attr-defined]
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ConfigError("YAML config must define a mapping at the top level")
        return data

    raise ConfigError(f"unsupported config extension: {config_path.suffix}")


__all__ = ["ConfigError", "load_config"]
