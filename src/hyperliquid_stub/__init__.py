"""Local stub namespace for Hyperliquid SDK (tests/offline).

This package provides minimal stand-ins used by unit tests and offline
development so that importing strategy modules does not require the real
SDK nor network access.
"""

from . import exchange as exchange  # re-export for type checkers
from . import info as info  # optional convenience
from . import utils as utils

__all__ = ["exchange", "info", "utils"]
