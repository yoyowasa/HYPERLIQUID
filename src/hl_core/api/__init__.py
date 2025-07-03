# src/hl_core/api/__init__.py
from __future__ import annotations
import json
import logging
import httpx
import websockets
import asyncio
import anyio
import contextlib
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
        self._ws: websockets.WebSocketClientProtocol | None = None

        # --- 追加 ---
        self._ready = asyncio.Event()  # open を待つため
        self._hb_task: asyncio.Task | None = None  # Heartbeat task

        # 購読リスト
        self._subs: list[str] = []
        # hook
        self.on_message = lambda _msg: None

    async def connect(self) -> None:
        """接続し listen を開始。切断されたら自動再接続（reconnect=True の場合）"""
        while True:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    logger.info("WS connected")
                    self._ready.set()  # ★ open 通知

                    # Heartbeat を 30 s ごとに送る
                    self._hb_task = asyncio.create_task(self._heartbeat())

                    # 過去の購読を復元
                    for ch in self._subs:
                        await self._ws.send(
                            json.dumps(
                                {"method": "subscribe", "subscription": {"type": ch}}
                            )
                        )

                    await self._listen()  # 切断までブロック
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
        """CloudFront idle-timeout 回避用のダミー Text を送信"""
        try:
            while True:
                await anyio.sleep(30)
                if self._ws and not getattr(self._ws, "closed", False):
                    await self._ws.send('{"type":"hb"}')
        except asyncio.CancelledError:
            pass

    async def _listen(self) -> None:
        async for msg in self._ws:  # type: ignore[operator]
            self.on_message(json.loads(msg))

    # -- 公開 API ----------------------------------------------------------

    async def wait_ready(self) -> None:
        """WS が open になるまで待機"""
        await self._ready.wait()

    async def subscribe(self, feed_type: str) -> None:
        """一度登録すれば `_subs` に残り、再接続時に自動で復元される。"""
        self._subs.add(feed_type)

        # まだ接続前、あるいは一度閉じたソケットなら今は送信せず復元待ち
        if not self._ws or getattr(self._ws, "closed", True):
            logger.warning("WS not connected; skip subscribe(%s)", feed_type)
            return

        await self._ws.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "subscription": {"type": feed_type},
                }
            )
        )


__all__ = ["HTTPClient", "WSClient"]
