# src/hl_core/api/__init__.py
from __future__ import annotations
import json
import logging
import httpx
import websockets
import asyncio
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
        url = f"/{path.lstrip('/')}"  # 末尾スラッシュずれを解消
        resp = await self._cli.get(url, params=params)
        resp.raise_for_status()  # 4xx / 5xx なら例外
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

    async def connect(self) -> None:
        """WS 接続し、受信ループをバックグラウンドで走らせる"""
        self._ws = await websockets.connect(self.url, ping_interval=None)
        asyncio.create_task(self._listen())   # 受信ループを常駐

    async def _listen(self) -> None:          # ★新規メソッド
        assert self._ws
        async for msg in self._ws:
            self.on_message(json.loads(msg))

    async def subscribe(self, feed_type: str) -> None:
        """
        Hyperliquid 公式仕様に従って購読メッセージを送信する。
        例: await ws.subscribe("allMids")
        """
        assert self._ws, "connect() を先に呼んでください"
        msg = {
            "method": "subscribe",
            "subscription": {"type": feed_type},
        }
        await self._ws.send(json.dumps(msg))

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()


__all__ = ["HTTPClient", "WSClient"]
