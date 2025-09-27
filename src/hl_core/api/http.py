# src/hl_core/api/http.py
# 〔このモジュールがすること〕
# HTTP ベースの発注アダプタを提供します。現状はシンプルなラッパーで、
# 発注の試行/成功/失敗を必ずログに残し、paper モードではダミー応答を返します。

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from hl_core.config import load_settings
from hl_core.utils.logger import get_logger

logger = get_logger("hl_core.api.http")


async def place_order(
    *,
    symbol: str,
    side: str,
    size: float,
    price: Optional[float] = None,
    time_in_force: str | None = None,
    order_type: Optional[str] = None,
    display_size: Optional[float] = None,
    iceberg: bool | None = None,
    stop_price: Optional[float] = None,
    ttl_s: Optional[float] = None,
    paper: bool | None = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Hyperliquid への発注（paper モードを含む）。"""

    tif = (time_in_force or extra.get("tif") or "").upper()
    kind = (order_type or extra.get("type") or ("STOP" if stop_price else "LIMIT")).upper()

    logger.info(
        "place_order: try symbol=%s side=%s type=%s tif=%s price=%s size=%s",
        symbol,
        side,
        kind,
        tif,
        price,
        size,
    )

    try:
        resp = await _place_order_impl(
            symbol=symbol,
            side=side,
            size=size,
            price=price,
            time_in_force=tif,
            order_type=kind,
            display_size=display_size,
            iceberg=iceberg,
            stop_price=stop_price,
            ttl_s=ttl_s,
            paper=paper,
            extra=extra,
        )
    except Exception as exc:  # pragma: no cover - runtime integration
        logger.error(
            "place_order: error symbol=%s side=%s price=%s size=%s err=%s",
            symbol,
            side,
            price,
            size,
            exc,
        )
        raise

    logger.info(
        "place_order: ok id=%s status=%s filled=%s remaining=%s",
        resp.get("order_id"),
        resp.get("status"),
        resp.get("filled"),
        resp.get("remaining"),
    )
    return resp


async def cancel_order(
    symbol: str,
    order_id: str,
    *,
    paper: bool | None = None,
    **extra: Any,
) -> Dict[str, Any]:
    """指定 ID の注文をキャンセルします（paper モードはログのみ）。"""

    logger.info("cancel_order: try symbol=%s order_id=%s", symbol, order_id)
    settings = load_settings()
    if paper or settings.dry_run:
        logger.info(
            "cancel_order: ok symbol=%s order_id=%s status=paper", symbol, order_id
        )
        return {"status": "paper", "order_id": order_id}

    raise RuntimeError("cancel_order adapter is not configured for live trading")


async def flatten_ioc(symbol: str, *, paper: bool | None = None, **extra: Any) -> None:
    """指定銘柄を IOC でクローズ（paper モードはログのみ）。"""

    logger.info("flatten_ioc: try symbol=%s", symbol)
    settings = load_settings()
    if paper or settings.dry_run:
        logger.info("flatten_ioc: ok symbol=%s status=paper", symbol)
        return None

    raise RuntimeError("flatten_ioc adapter is not configured for live trading")


async def _place_order_impl(
    *,
    symbol: str,
    side: str,
    size: float,
    price: Optional[float],
    time_in_force: str,
    order_type: str,
    display_size: Optional[float],
    iceberg: Optional[bool],
    stop_price: Optional[float],
    ttl_s: Optional[float],
    paper: Optional[bool],
    extra: Dict[str, Any],
) -> Dict[str, Any]:
    """内部ヘルパー: 実際の送信処理（paper モードではダミー応答）。"""

    settings = load_settings()
    if paper or settings.dry_run:
        await asyncio.sleep(0)
        order_id = f"paper-{side.lower()}-{int(time.time() * 1000)}"
        return {
            "status": "paper",
            "order_id": order_id,
            "filled": 0.0,
            "remaining": float(size),
        }

    raise RuntimeError("place_order adapter is not configured for live trading")


__all__ = ["place_order", "cancel_order", "flatten_ioc"]

