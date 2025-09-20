# src/hl_core/api/__init__.py
from __future__ import annotations
import json
import logging
import httpx
import websockets
import asyncio
import anyio
from typing import Awaitable, Callable, Any, Optional
import ssl

logger = logging.getLogger(__name__)


class HTTPClient:
    """
    Hyperliquid REST API ラッパ（雛形）
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        verify: bool | str | ssl.SSLContext = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._cli = httpx.AsyncClient(base_url=self.base_url, verify=verify)
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

        self._ws: Any | None = None
        # WebSocket 購読情報を JSON 文字列で保持（reconnect 時に再送）
        self._subs: set[str] = set()

        # コールバック（デフォルトは no-op）
        self.on_message: Callable[[dict[str, Any]], Awaitable[None] | None] = (
            lambda _m: None
        )

        # 接続状態を示すフラグ（asyncio.Event）
        self._ready: asyncio.Event = asyncio.Event()

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

                    # 過去の購読を復元
                    for sub_json in self._subs:
                        try:
                            subscription = json.loads(sub_json)
                        except json.JSONDecodeError:
                            logger.warning(
                                "Skip invalid stored subscription: %s", sub_json
                            )
                            continue
                        await ws.send(
                            json.dumps(
                                {"method": "subscribe", "subscription": subscription}
                            )
                        )

                    await self._listen()  # 切断までブロック
            except Exception as exc:
                logger.warning("WS disconnected: %s", exc)
            finally:
                self._ready.clear()  # ★ close を通知

            if not self.reconnect:
                break
            await anyio.sleep(self.retry_sec)

    # ── 3. wait_ready ───────────────────────────────────────────
    async def wait_ready(self) -> None:
        """WS が open になるまで待機"""
        await self._ready.wait()

    # ── 4. subscribe ────────────────────────────────────────────
    @staticmethod
    def _normalize_subscription(
        subscription: str | dict[str, Any]
    ) -> tuple[dict[str, Any], str, str]:
        """返却: (payload, key, label)"""

        if isinstance(subscription, str):
            payload: dict[str, Any] = {"type": subscription}
            label = subscription
        elif isinstance(subscription, dict):
            payload = dict(subscription)
            label = str(payload.get("type", payload))
        else:  # pragma: no cover - 型ヒントがあるため通常到達しない
            raise TypeError(f"Unsupported subscription type: {type(subscription)!r}")

        key = json.dumps(payload, sort_keys=True)
        return payload, key, label

    async def subscribe(self, subscription: str | dict[str, Any]) -> None:
        payload, sub_key, label = self._normalize_subscription(subscription)

        # まだ接続完了していない場合は待つ
        if not self._ready.is_set():
            logger.warning("WS not ready; skip subscribe(%s)", label)
            return

        if sub_key in self._subs:
            return  # 既に購読済み

        ws = self._ws
        if ws is None:
            logger.warning("WS handle missing; skip subscribe(%s)", label)
            return

        await ws.send(json.dumps({"method": "subscribe", "subscription": payload}))
        self._subs.add(sub_key)
        logger.debug("Subscribed %s", label)

    # ─────────────────────────────────────────────────────────────
    async def _listen(self) -> None:
        """
        サーバーから届く WebSocket メッセージを
        JSON デコードして on_message フックへ渡す。
        """
        ws = self._ws
        if ws is None:
            return

        async for raw in ws:  # noqa: E501  type: ignore[operator]
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
        ws = self._ws
        if ws and not getattr(ws, "closed", False):
            await ws.close()
        self._ready.clear()
        self._ws = None
        logger.info("WS closed")


__all__ = ["HTTPClient", "WSClient"]
