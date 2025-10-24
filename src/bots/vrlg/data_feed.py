"""Data feed for the VRLG strategy.

This module streams level-1 book data either from the real Hyperliquid
WebSocket or from a synthetic generator (when the adapter is unavailable),
and periodically converts it into ``FeatureSnapshot`` objects that feed the
strategy pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib  # 〔この import がすること〕 タスクキャンセル時の例外抑止に使います
import math
import time
from dataclasses import dataclass, replace
from collections.abc import AsyncIterator, Callable
from typing import Any, Optional, Tuple, cast

import logging

logger = logging.getLogger("bots.vrlg.data")


@dataclass(frozen=True)
class FeatureSnapshot:
    """A 100ms feature sample consumed by the strategy.

    Attributes
    ----------
    t:
        Generation timestamp in epoch seconds.
    mid:
        Mid-price derived from the best bid/ask.
    spread_ticks:
        Spread normalised by the configured tick size.
    dob:
        "Depth on book" – the sum of the best bid and ask sizes.
    obi:
        Order-book imbalance, normalised by ``dob``.
    block_phase:
        Phase hint supplied later by the rotation detector.
    """

    t: float
    mid: float
    spread_ticks: float
    dob: float
    obi: float
    block_phase: Optional[float] = None

    def with_phase(self, phase: float) -> "FeatureSnapshot":
        """Return a copy of this snapshot with ``block_phase`` set."""

        return replace(self, block_phase=float(phase))


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Fetch ``key`` from either an attribute or mapping, safely."""

    try:
        return getattr(obj, key)
    except Exception:
        try:
            return obj[key]  # type: ignore[index]
        except Exception:
            return default


SubscribeLevel2 = Callable[[str], AsyncIterator[Any]]


async def _subscribe_level1(
    symbol: str,
    out: "asyncio.Queue[Tuple[float, float, float, float]]",
    subscribe_level2: SubscribeLevel2 | None = None,
) -> None:
    """Stream best bid/ask quotes from the exchange WebSocket."""

    if subscribe_level2 is None or not callable(subscribe_level2):
        try:
            from hl_core.api.ws import subscribe_level2 as subscribe_level2_fn  # type: ignore
        except Exception as exc:
            logger.warning("level2 WS adapter unavailable: %s", exc)
            raise
    else:
        subscribe_level2_fn = subscribe_level2

    subscribe_level2_fn = cast(SubscribeLevel2, subscribe_level2_fn)

    try:
        async for book in subscribe_level2_fn(symbol):
            bb = float(_get(book, "best_bid", 0.0))
            ba = float(_get(book, "best_ask", 0.0))
            bs = float(_get(book, "bid_size_l1", 0.0))
            asz = float(_get(book, "ask_size_l1", 0.0))
            await out.put((bb, ba, bs, asz))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("level2 stream failed: %s", exc)
        raise


async def _synthetic_level1(
    out: "asyncio.Queue[Tuple[float, float, float, float]]",
    tick: float,
) -> None:
    """Emit synthetic level-1 quotes with boundary structure for testing."""

    dt = 0.05  # 50ms resolution to ensure smooth 100ms sampling.
    base_mid = 70_000.0
    period_s = 2.0
    i = 0
    try:
        while True:
            phase = ((i * dt) % period_s) / period_s
            boundary = phase < 0.15 or phase > 0.85
            spread_ticks = 3.0 if boundary else 1.0
            spread = spread_ticks * max(tick, 1e-12)
            mid = base_mid * (1.0 + 0.00001 * math.sin(2.0 * math.pi * i / 997.0))
            best_bid = mid - spread / 2.0
            best_ask = mid + spread / 2.0
            bid_size = 600.0 if boundary else 1_200.0
            ask_size = 600.0 if boundary else 1_200.0
            await out.put((best_bid, best_ask, bid_size, ask_size))
            i += 1
            await asyncio.sleep(dt)
    except asyncio.CancelledError:
        raise


async def _feature_pump(
    cfg,
    out_queue: "asyncio.Queue[FeatureSnapshot]",
    lv1_queue: "asyncio.Queue[Tuple[float, float, float, float]]",
) -> None:
    """Sample the latest level-1 data every 100ms and emit features."""


    tick = float(getattr(getattr(cfg, "symbol", {}), "tick_size", 0.5))
    dt = 0.1  # 100ms cadence
    last = (0.0, 0.0, 0.0, 0.0)
    last_mid = 0.0
    next_ts = time.time()

    try:
        while True:
            try:
                while True:
                    last = lv1_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass

            bb, ba, bs, asz = last
            if bb > 0.0 and ba > 0.0:
                last_mid = (bb + ba) / 2.0
                spread_ticks = (ba - bb) / max(tick, 1e-12)
            else:
                spread_ticks = 0.0

            if last_mid <= 0.0:
                await asyncio.sleep(dt)
                next_ts = time.time() + dt
                continue

            dob = bs + asz
            obi = 0.0 if dob <= 0.0 else (bs - asz) / max(dob, 1e-9)

            snap = FeatureSnapshot(
                t=time.time(),
                mid=last_mid,
                spread_ticks=spread_ticks,
                dob=dob,
                obi=obi,
            )

            try:
                out_queue.put_nowait(snap)
            except asyncio.QueueFull:
                try:
                    _ = out_queue.get_nowait()
                except Exception:
                    pass
                try:
                    out_queue.put_nowait(snap)
                except Exception:
                    pass

            next_ts += dt
            await asyncio.sleep(max(0.0, next_ts - time.time()))
    except asyncio.CancelledError:
        raise


async def run_feeds(cfg, out_queue: "asyncio.Queue[FeatureSnapshot]") -> None:
    """〔この関数がすること〕
    - L2（最良気配）を購読して内部キューへ格納
    - 100ms ごとに特徴量を生成して out_queue へ流す
    - **WS が途中で落ちても** 合成フィードへ自動フォールバック
    - タスクはキャンセルで安全に停止します
    """
    symbol = getattr(getattr(cfg, "symbol", {}), "name", "BTCUSD-PERP")
    tick = float(getattr(getattr(cfg, "symbol", {}), "tick_size", 0.5))
    lv1_queue: "asyncio.Queue[tuple[float,float,float,float]]" = asyncio.Queue(maxsize=1024)

    # 100ms 特徴生成は固定で起動
    pump_task = asyncio.create_task(_feature_pump(cfg, out_queue, lv1_queue), name="feature_pump")

    # まずは WS を試みる
    producer_task = asyncio.create_task(_subscribe_level1(symbol, lv1_queue), name="l2_subscriber")
    producer_label = "ws"

    try:
        while True:
            # いずれかのタスクが例外で落ちたら処理
            done, _ = await asyncio.wait({pump_task, producer_task}, return_when=asyncio.FIRST_EXCEPTION)

            # 生成側（pump）が落ちた → すべて終了
            if pump_task in done:
                exc = pump_task.exception()
                if producer_task and not producer_task.done():
                    producer_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await producer_task
                if exc:
                    raise exc
                break

            # ここまで来たら producer 側が終了（例外含む）
            exc = producer_task.exception()
            if producer_label == "ws":
                # WS が死んだ → 合成フィードへ切り替え
                logger.warning("level2 WS failed (%s); switching to synthetic L1.", exc)
            else:
                logger.warning("synthetic L1 stopped unexpectedly (%s); restarting.", exc)

            # 合成フィードを起動し直してループ継続
            producer_task = asyncio.create_task(_synthetic_level1(lv1_queue, tick), name="l1_synth")
            producer_label = "synthetic"

    except asyncio.CancelledError:
        # 正常停止（外部キャンセル）
        pass
    finally:
        # タスク後片付け
        for t in (producer_task, pump_task):
            if t and not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
