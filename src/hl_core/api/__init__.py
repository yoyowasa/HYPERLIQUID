# src/hl_core/api/__init__.py
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class HTTPClient:
    """
    Hyperliquid REST API ラッパ（雛形）
    """

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        logger.debug("HTTPClient initialised: %s", self.base_url)

    async def get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:  # noqa: D401
        """
        非同期 GET（実装は後で）"""
        pass

    async def post(self, endpoint: str, data: dict[str, Any] | None = None) -> Any:  # noqa: D401
        """
        非同期 POST（実装は後で）"""
        pass


class WSClient:
    """
    Hyperliquid WebSocket ラッパ（雛形）
    """

    def __init__(self, url: str, reconnect: bool = True) -> None:
        self.url = url
        self.reconnect = reconnect
        logger.debug("WSClient initialised: %s", self.url)

    async def connect(self) -> None:  # noqa: D401
        """接続（後で実装）"""
        pass

    async def subscribe(self, channel: str, params: dict[str, Any] | None = None) -> None:  # noqa: D401
        """購読（後で実装）"""
        pass

    async def close(self) -> None:  # noqa: D401
        """切断（後で実装）"""
        pass


__all__ = ["HTTPClient", "WSClient"]
