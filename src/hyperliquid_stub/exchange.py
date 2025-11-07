"""Minimal Hyperliquid Exchange stub used for tests.

This provides only the attributes and methods exercised by the unit
tests so that strategies depending on the official SDK can be
constructed without performing real network I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class _Info:
    """Stub for ``Exchange.info`` namespace."""

    def meta(self) -> dict[str, Any]:
        # Return just enough structure for PFPLStrategy initialisation.
        return {
            "universe": [
                {
                    "name": "ETH",
                    "pxTick": "0.01",
                    "qtyTick": "0.001",
                    "szDecimals": 3,
                }
            ],
            "minSizeUsd": {"ETH": "1"},
        }

    def user_state(self, account: str) -> dict[str, Any]:
        # Minimal user state with no positions.
        return {"perpPositions": []}


class Exchange:
    """Very small subset of the real Hyperliquid SDK's Exchange class."""

    def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - trivial
        self.info = _Info()
