# src/hl_core/api/__init__.py
from __future__ import annotations
import json
import logging
import httpx
import websockets
import asyncio
from typing import Awaitable, Callable, Any, Optional

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
    def __init__(
        self,
        url: str,
        *,
        reconnect: bool = False,
        retry_sec: float = 3.0,
    ) -> None:
        self.url = url
        self.reconnect = reconnect
        self.retry_sec = retry_sec
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._subs: list[str] = []  # ★購読チャンネル保持
        self.on_message: Callable[[dict[str, Any]], Awaitable[None] | None] = (
            lambda _m: None
        )
        logger.debug("WSClient initialised: %s", self.url)

    async def connect(self) -> None:
        """接続して listen を開始。切断時は自動再接続。"""
        self._hb_task: asyncio.Task | None = None  # Heartbeat タスク
        while True:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=20,  # → CloudFront 推奨値
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    logger.info("WS connected")

                    # 30 s ごとにダミー Text を送る Heartbeat を開始
                    self._hb_task = asyncio.create_task(self._heartbeat())

                    # 直前の購読を復元
                    for ch in self._subs:
                        await self._ws.send(
                            json.dumps(
                                {"method": "subscribe", "subscription": {"type": ch}}
                            )
                        )

                    await self._listen()  # ← 切断までブロック
            except Exception as exc:
                logger.warning("WS disconnected: %s", exc)
            finally:
                if self._hb_task:  # Heartbeat 停止
                    self._hb_task.cancel()
                    with contextlib.suppress(Exception):
                        await self._hb_task
                    self._hb_task = None
                self._ready.clear()

            if not self.reconnect:
                break
            await anyio.sleep(self.retry_sec)

    async def _heartbeat(self) -> None:
        """CloudFront idle-timeout を回避するダミー送信"""
        try:
            while True:
                await anyio.sleep(30)
                if self._ws and not getattr(self._ws, "closed", False):
                    await self._ws.send('{"type":"hb"}')
        except asyncio.CancelledError:
            pass

    async def _listen(self) -> None:
        async for msg in self._ws:  # type: ignore[operator]
            if self.on_message:
                self.on_message(json.loads(msg))

    async def subscribe(self, feed_type: str) -> None:
        """未接続時はスキップ; 接続後 self._subs へ記録"""
        if not self._ws or getattr(self._ws, "closed", True):
            logger.warning("WS not connected; skip subscribe(%s)", feed_type)
            return
        await self._ws.send(
            json.dumps({"method": "subscribe", "subscription": {"type": feed_type}})
        )
        if feed_type not in self._subs:
            self._subs.append(feed_type)  # ★記録

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
            logger.info("WS closed")


__all__ = ["HTTPClient", "WSClient"]
