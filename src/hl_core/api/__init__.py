# src/hl_core/api/__init__.py
from __future__ import annotations

import logging
import httpx

from typing import Any, Optional

logger = logging.getLogger(__name__)


class HTTPClient:
    """
    Hyperliquid REST API ラッパ（雛形）
    """

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._cli = httpx.AsyncClient(base_url=self.base_url)
        logger.debug("HTTPClient initialised: %s", self.base_url)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """
        指定パスに GET リクエストを送り、JSON を返す。
        例: await cli.get("v1/markets")
        """
        url = f"/{path.lstrip('/')}"               # 末尾スラッシュずれを解消
        resp = await self._cli.get(url, params=params)
        resp.raise_for_status()                    # 4xx / 5xx なら例外
        return resp.json()

    async def post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        url = f"/{path.lstrip('/')}"
        resp = await self._cli.post(url, json=data or {})
        resp.raise_for_status()
        return resp.json()
    async def close(self) -> None:
        await self._cli.aclose()


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
