from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional, Tuple

from hl_core.config import load_settings, require_live_creds
from hl_core.utils.logger import get_logger

# HTTP 発注アダプタ
# - paper（DRY_RUN）時はダミー応答
# - Live は GO 環境変数で安全ゲートし、SDK 経由で発注

logger = get_logger("hl_core.api.http")


def _base_coin(symbol: str) -> str:
    """シンボルから基軸コイン（BTC/ETH 等）を抽出。"""
    token = (symbol or "BTC").split("-", 1)[0].split("/", 1)[0]
    return (token[:-3] if token.endswith("USD") else token).upper() or "BTC"


def _go_live_enabled() -> bool:
    """安全ゲート: GO=1|true|live|go のみ Live を許可。"""
    val = (os.getenv("GO") or "").strip().lower()
    return val in {"1", "true", "live", "go"}


def _extract_order_id(ack: Any) -> str | None:
    """SDK/スタブ応答から最善で order_id を抽出。"""
    try:
        statuses = (
            (ack or {}).get("response", {}).get("data", {}).get("statuses", [])
        )
        if statuses:
            oid = statuses[0].get("oid") or statuses[0].get("orderId")
            if oid is not None:
                return str(oid)
    except Exception:
        pass
    try:
        oid = (ack or {}).get("order_id")
        if oid is not None:
            return str(oid)
    except Exception:
        pass
    return None


def _build_sdk_kwargs(
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
    reduce_only: Optional[bool],
    post_only: Optional[bool],
) -> Tuple[tuple, dict]:
    """Exchange.order へ渡す標準化 kwargs（SDK 差分に備え superset を構築）。"""
    coin = _base_coin(symbol)
    is_buy = str(side).upper() == "BUY"
    args: tuple = ()
    kwargs: dict[str, Any] = {"coin": coin, "is_buy": is_buy, "sz": float(size)}
    if price is not None:
        kwargs["limit_px"] = float(price)
    if stop_price is not None:
        kwargs["stop_px"] = float(stop_price)
    if display_size is not None:
        kwargs["display_size"] = float(display_size)
    if iceberg:
        kwargs["iceberg"] = True
    if post_only:
        kwargs["post_only"] = True
    if reduce_only:
        kwargs["reduce_only"] = True
    if ttl_s:
        kwargs["ttl_s"] = float(ttl_s)
    if time_in_force:
        kwargs["tif"] = str(time_in_force).upper()
    if order_type:
        kwargs["type"] = str(order_type).upper()
    return args, kwargs


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
    reduce_only: bool | None = None,
    post_only: bool | None = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Hyperliquid への発注（paper/Live 共通）。"""

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
            reduce_only=reduce_only,
            post_only=post_only,
            extra=extra,
        )
    except Exception as exc:  # pragma: no cover - ランタイム依存
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
    """注文 ID のキャンセル（paper はログのみ）。"""

    logger.info("cancel_order: try symbol=%s order_id=%s", symbol, order_id)
    settings = load_settings()
    if paper or settings.dry_run:
        logger.info(
            "cancel_order: ok symbol=%s order_id=%s status=paper", symbol, order_id
        )
        return {"status": "paper", "order_id": order_id}

    # Live は SDK 差異が大きいため未実装（安全のため拒否）
    raise RuntimeError("cancel_order live path is not implemented yet")


async def flatten_ioc(symbol: str, *, paper: bool | None = None, **extra: Any) -> None:
    """全建玉を IOC でクローズ（paper はログのみ）。"""

    logger.info("flatten_ioc: try symbol=%s", symbol)
    settings = load_settings()
    if paper or settings.dry_run:
        logger.info("flatten_ioc: ok symbol=%s status=paper", symbol)
        return None

    # Live は未実装（安全のため拒否）
    raise RuntimeError("flatten_ioc live path is not implemented yet")


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
    reduce_only: Optional[bool],
    post_only: Optional[bool],
    extra: Dict[str, Any],
) -> Dict[str, Any]:
    """paper/Live を切替えて発注を実施。"""

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

    # --- Live path ---
    if not _go_live_enabled():
        raise ValueError("GO が未設定のため Live 発注を拒否しました")
    require_live_creds(settings)

    # SDK（またはローカルスタブ）経由で発注
    from hl_core.hl_client import make_clients  # 遅延 import

    _info, exchange, _addr = make_clients(settings)
    if exchange is None:  # pragma: no cover - 防御
        raise RuntimeError("Exchange の初期化に失敗しました")

    _args, kwargs = _build_sdk_kwargs(
        symbol=symbol,
        side=side,
        size=size,
        price=price,
        time_in_force=time_in_force,
        order_type=order_type,
        display_size=display_size,
        iceberg=iceberg,
        stop_price=stop_price,
        ttl_s=ttl_s,
        reduce_only=reduce_only,
        post_only=post_only,
    )

    ack = await asyncio.to_thread(getattr(exchange, "order"), *(), **kwargs)  # type: ignore[misc]
    oid = _extract_order_id(ack) or f"live-{int(time.time()*1000)}"
    return {
        "status": "live",
        "order_id": oid,
        "filled": 0.0,
        "remaining": float(size),
        "raw": ack,
    }


__all__ = ["place_order", "cancel_order", "flatten_ioc"]

