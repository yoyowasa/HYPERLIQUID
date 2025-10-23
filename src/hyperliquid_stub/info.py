from __future__ import annotations

from typing import Any


class Info:
    """Minimal stub of the SDK's Info client used in tools/tests.

    Accepts arbitrary constructor arguments and exposes ``meta`` with a
    static response sufficient for tick/size rounding helpers.
    """

    def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - trivial
        pass

    def meta(self) -> dict[str, Any]:
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

