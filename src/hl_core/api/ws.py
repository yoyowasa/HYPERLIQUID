"""Async Hyperliquid WebSocket adapters."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, Dict, List

import websockets

from hl_core.config import load_settings

logger = logging.getLogger(__name__)

_MAINNET_WSS = "wss://api.hyperliquid.xyz/ws"
_TESTNET_WSS = "wss://api.hyperliquid-testnet.xyz/ws"


def _ws_url() -> str:
    override = os.getenv("HL_WS_URL")
    if override:
        return override
    try:
        settings = load_settings()
        network = (settings.network or "testnet").strip().lower()
    except Exception:
        network = "testnet"
    return _MAINNET_WSS if network == "mainnet" else _TESTNET_WSS


def _base_coin(symbol: str | None) -> str:
    if not symbol:
        return "BTC"
    token = symbol.split("-", 1)[0]
    token = token.split("/", 1)[0]
    if token.endswith("USD"):
        token = token[:-3]
    return token.upper() or "BTC"


async def _ws_stream(subscription: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
    uri = _ws_url()
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"method": "subscribe", "subscription": subscription}))
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    channel = msg.get("channel")
                    if channel in {"subscriptionResponse", "pong"}:
                        continue
                    if channel == "error":
                        raise RuntimeError(msg.get("data"))
                    yield msg
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning(
                "ws subscription %s closed (%s); reconnecting in %.1fs",
                subscription.get("type"),
                exc,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)


def _best_level(levels: List[Dict[str, Any]]) -> tuple[float | None, float | None]:
    if not levels:
        return None, None
    first = levels[0] or {}
    px_val = first.get("px")
    sz_val = first.get("sz")
    if px_val is None or sz_val is None:
        return None, None
    try:
        px = float(px_val)
        sz = float(sz_val)
    except Exception:
        return None, None
    return px, sz


async def subscribe_level2(symbol: str) -> AsyncIterator[Dict[str, Any]]:
    """Yield best bid/ask snapshots for ``symbol``."""

    coin = _base_coin(symbol)
    subscription = {"type": "l2Book", "coin": coin}
    async for msg in _ws_stream(subscription):
        if msg.get("channel") != "l2Book":
            continue
        data = msg.get("data") or {}
        if data.get("coin", "").upper() != coin:
            continue
        try:
            bids, asks = data.get("levels", ([{}], [{}]))
        except Exception:
            continue
        bid_px, bid_sz = _best_level(bids)
        ask_px, ask_sz = _best_level(asks)
        yield {
            "coin": coin,
            "best_bid": bid_px,
            "best_ask": ask_px,
            "bid_size_l1": bid_sz,
            "ask_size_l1": ask_sz,
            "timestamp": float(data.get("time", 0)) / 1000.0,
            "raw": data,
        }


async def subscribe_trades(symbol: str) -> AsyncIterator[Dict[str, Any]]:
    """Yield individual public trades for ``symbol``."""

    coin = _base_coin(symbol)
    subscription = {"type": "trades", "coin": coin}
    async for msg in _ws_stream(subscription):
        if msg.get("channel") != "trades":
            continue
        trades = msg.get("data") or []
        for trade in trades:
            if trade.get("coin", "").upper() != coin:
                continue
            try:
                side = "BUY" if trade.get("side") == "A" else "SELL"
                price = float(trade.get("px"))
                size = float(trade.get("sz"))
                ts = float(trade.get("time", 0)) / 1000.0
            except Exception:
                continue
            yield {
                "coin": coin,
                "side": side,
                "price": price,
                "size": size,
                "t": ts,
                "raw": trade,
            }


async def subscribe_fills(symbol: str, address: str | None = None) -> AsyncIterator[Dict[str, Any]]:
    """Yield private fills for ``symbol`` filtered by ``address``."""

    coin = _base_coin(symbol)
    if address is None:
        settings = load_settings()
        address = settings.account_address
    if not address:
        raise RuntimeError(
            "subscribe_fills requires HL_ACCOUNT_ADDRESS (set in .env) when paper/live fills are needed"
        )

    subscription = {"type": "userFills", "user": address}
    async for msg in _ws_stream(subscription):
        if msg.get("channel") != "userFills":
            continue
        data = msg.get("data") or {}
        fills = data.get("fills", [])
        for fill in fills:
            if fill.get("coin", "").upper() != coin:
                continue
            try:
                price = float(fill.get("px"))
                size = float(fill.get("sz"))
                ts = float(fill.get("time", 0)) / 1000.0
                side = "BUY" if fill.get("side") == "B" else "SELL"
                order_id = str(fill.get("oid")) if fill.get("oid") is not None else None
            except Exception:
                continue
            yield {
                "coin": coin,
                "side": side,
                "price": price,
                "size": size,
                "t": ts,
                "order_id": order_id,
                "raw": fill,
            }


async def subscribe_blocks(symbol: str) -> AsyncIterator[Dict[str, Any]]:
    """Approximate block events using ``activeAssetCtx`` updates for ``symbol``."""

    coin = _base_coin(symbol)
    subscription = {"type": "activeAssetCtx", "coin": coin}
    async for msg in _ws_stream(subscription):
        if msg.get("channel") not in {"activeAssetCtx", "activeSpotAssetCtx"}:
            continue
        yield {
            "coin": coin,
            "timestamp": time.time(),
            "data": msg.get("data"),
        }
