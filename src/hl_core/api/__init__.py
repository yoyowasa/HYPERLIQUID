# src/hl_core/api/__init__.py
from __future__ import annotations
import json
import logging
import httpx
import websockets
import asyncio
import anyio
import contextlib
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
    # ── 1. __init__ ─────────────────────────────────────────────
    def __init__(self, url: str, reconnect: bool = False, retry_sec: float = 3.0):
        self.url = url
        self.reconnect = reconnect
        self.retry_sec = retry_sec

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._subs: set[str] = set()  # set に統一
        self._hb_task: asyncio.Task | None = None

        # コールバック（デフォルトは no-op）
        self.on_message: Callable[[dict[str, Any]], Awaitable[None] | None] = (
            lambda _m: None
        )

        # 接続状態を示すフラグ（asyncio.Event）
        self._ready: asyncio.Event = asyncio.Event()

    # ─────────────────────────────────────────────────────────────
    async def _heartbeat(self) -> None:
        """
        30 秒ごとにダミー ping を送信して CloudFront のアイドル
        タイムアウト（約 60 s）を回避する。
        """
        try:
            while True:
                # Python 3.13 なので anyio.sleep が素直に使える
                await anyio.sleep(30)
                # self._ws がまだ生きていれば送信
                if self._ws and not getattr(self._ws, "closed", False):
                    await self._ws.send('{"type":"hb"}')
        except asyncio.CancelledError:
            # connect() 側で self._hb_task.cancel() されたときに抜ける
            pass

    # ─────────────────────────────────────────────────────────────

    # ── 2. connect ──────────────────────────────────────────────
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
                    self._ready.set()  # ★ open を通知

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
                self._ready.clear()  # ★ close を通知

            if not self.reconnect:
                break
            await anyio.sleep(self.retry_sec)

    # ── 3. wait_ready ───────────────────────────────────────────
    async def wait_ready(self) -> None:
        """WS が open になるまで待機"""
        await self._ready.wait()

    # ── 4. subscribe ────────────────────────────────────────────
    async def subscribe(self, feed_type: str) -> None:
        # まだ接続完了していない場合は待つ
        if not self._ready.is_set():
            logger.warning("WS not ready; skip subscribe(%s)", feed_type)
            return

        if feed_type in self._subs:
            return  # 既に購読済み

        await self._ws.send(
            json.dumps({"method": "subscribe", "subscription": {"type": feed_type}})
        )
        self._subs.add(feed_type)
        logger.debug("Subscribed %s", feed_type)

    # ─────────────────────────────────────────────────────────────
    async def _listen(self) -> None:
        """
        サーバーから届く WebSocket メッセージを
        JSON デコードして on_message フックへ渡す。
        """
        async for raw in self._ws:  # noqa: E501  type: ignore[operator]
            try:
                msg = json.loads(raw)
            except Exception as exc:
                logger.warning("WS message decode error: %s (%s)", exc, raw[:120])
                continue

            # ユーザー定義フック（同期でも async でも OK）
            cb_ret = self.on_message(msg)
            if asyncio.iscoroutine(cb_ret):
                await cb_ret

    # ─────────────────────────────────────────────────────────────

    # ── 5. close ────────────────────────────────────────────────
    async def close(self) -> None:
        if self._hb_task:
            self._hb_task.cancel()
        if self._ws and not getattr(self._ws, "closed", False):
            await self._ws.close()
        self._ready.clear()
        logger.info("WS closed")


__all__ = ["HTTPClient", "WSClient"]
