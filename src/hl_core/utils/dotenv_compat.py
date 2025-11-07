"""Compatibility layer for optional :mod:`python-dotenv` usage.

This repository historically depends on the third-party ``python-dotenv``
package for loading ``.env`` files as well as for the optional ``python -m
dotenv run`` command.  However, several environments in which the bots are run
do not always have the dependency (or its ``[cli]`` extra) installed.  Importing
``load_dotenv`` directly therefore raises :class:`ModuleNotFoundError`, causing
early termination before any helpful message can be shown.

The helpers below offer a light-weight fallback: we try to import the real
package at runtime and delegate to it when available; otherwise we provide a
minimal ``load_dotenv`` implementation that supports the subset of behaviour
needed by the bots (plain ``KEY=VALUE`` parsing with optional overrides).  This
keeps the runtime resilient while still allowing full functionality when the
real dependency is installed.
"""

from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from typing import Iterator


def _strip_inline_comment(value: str) -> str:
    """Remove unquoted ``#`` comments from a value string."""

    quote: str | None = None
    result: list[str] = []
    for ch in value:
        if ch in {'"', "'"}:
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
            result.append(ch)
            continue
        if ch == "#" and quote is None:
            break
        result.append(ch)
    return "".join(result)


def _parse_line(line: str) -> tuple[str, str] | None:
    """Parse a single ``.env`` line into ``(key, value)`` if possible."""

    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    if stripped.startswith("export "):
        stripped = stripped[len("export ") :]

    if "=" not in stripped:
        return None

    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None

    raw_value = _strip_inline_comment(raw_value).strip()
    if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {'"', "'"}:
        value = raw_value[1:-1]
    else:
        value = raw_value
    return key, value


def _iter_env_lines(path: Path) -> Iterator[tuple[str, str]]:
    """Yield parsed environment assignments from ``path``."""

    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                parsed = _parse_line(raw)
                if parsed:
                    yield parsed
    except FileNotFoundError:
        return


def _fallback_load(path: Path, *, override: bool) -> bool:
    """Fallback loader when :mod:`python-dotenv` is unavailable."""

    updated = False
    for key, value in _iter_env_lines(path):
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        updated = True
    return updated


def load_dotenv(
    dotenv_path: str | os.PathLike[str] | None = None,
    *,
    override: bool | None = None,
) -> bool:
    """Load environment variables from ``.env``.

    When ``python-dotenv`` is installed we delegate to its ``load_dotenv``
    implementation.  Otherwise a minimal parser is used so that the bots can
    continue booting even without the optional dependency.
    """

    try:
        module = import_module("dotenv")
    except ModuleNotFoundError:
        module = None

    path = Path(dotenv_path) if dotenv_path else Path.cwd() / ".env"
    effective_override = bool(override) if override is not None else False

    if module and hasattr(module, "load_dotenv"):
        load = getattr(module, "load_dotenv")
        if callable(load):
            if dotenv_path is None:
                return bool(load(override=effective_override))
            return bool(load(dotenv_path, override=effective_override))

    return _fallback_load(path, override=effective_override)


__all__ = ["load_dotenv"]
