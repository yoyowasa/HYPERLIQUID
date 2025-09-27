"""Data feed for the VRLG strategy.

This module streams level-1 book data either from the real Hyperliquid
WebSocket or from a synthetic generator (when the adapter is unavailable),
and periodically converts it into ``FeatureSnapshot`` objects that feed the
strategy pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from dataclasses import dataclass, replace
from typing import Any, Optional, Tuple

from hl_core.utils.logger import get_logger

logger = get_logger("VRLG.data")


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


async def _subscribe_level1(
    symbol: str,
    out: "asyncio.Queue[Tuple[float, float, float, float]]",
    subscribe_level2,
) -> None:
    """Stream best bid/ask quotes from the exchange WebSocket."""

    try:
        async for book in subscribe_level2(symbol):
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
    """Run the VRLG data feed with automatic synthetic fallback."""

    symbol = getattr(getattr(cfg, "symbol", {}), "name", "BTCUSD-PERP")
    tick = float(getattr(getattr(cfg, "symbol", {}), "tick_size", 0.5))
    lv1_queue: "asyncio.Queue[Tuple[float, float, float, float]]" = asyncio.Queue(maxsize=1024)

    try:
        from hl_core.api.ws import subscribe_level2  # type: ignore
    except Exception as exc:
        logger.warning("level2 WS adapter unavailable: %s; using synthetic L1", exc)
        producer_task: Optional[asyncio.Task] = asyncio.create_task(
            _synthetic_level1(lv1_queue, tick),
            name="l1_synth",
        )
        using_synthetic = True
    else:
        producer_task = asyncio.create_task(
            _subscribe_level1(symbol, lv1_queue, subscribe_level2),
            name="l2_subscriber",
        )
        using_synthetic = False

        # Allow immediate import/connection failures to surface so we can fall back.
        await asyncio.sleep(0)

        if producer_task.done() and producer_task.exception():
            logger.warning(
                "level2 subscription failed instantly (%s); switching to synthetic feed",
                producer_task.exception(),
            )
            producer_task = asyncio.create_task(
                _synthetic_level1(lv1_queue, tick),
                name="l1_synth",
            )

            using_synthetic = True

    pump_task = asyncio.create_task(
        _feature_pump(cfg, out_queue, lv1_queue),
        name="feature_pump",
    )

    tasks = [pump_task, producer_task]

    try:
        while tasks:
            done, pending = await asyncio.wait(
                [t for t in tasks if t is not None],
                return_when=asyncio.FIRST_EXCEPTION,
            )

            # If the producer failed and we were using WS, fall back to synthetic once.

            if producer_task in done and producer_task and producer_task.exception():
                if not using_synthetic:
                    logger.warning(
                        "level2 stream failed (%s); falling back to synthetic feed",
                        producer_task.exception(),
                    )
                    producer_task = asyncio.create_task(
                        _synthetic_level1(lv1_queue, tick),
                        name="l1_synth",
                    )
                    using_synthetic = True
                    tasks = [pump_task, producer_task]
                    continue
                raise producer_task.exception()


            # If any task completed without exception we simply exit (likely cancellation).
            if any(t.exception() for t in done if t is not producer_task):
                for exc_task in done:
                    exc = exc_task.exception()
                    if exc:
                        raise exc
            break
    except asyncio.CancelledError:
        raise
    finally:
        for t in tasks:
            if t:
                t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*[t for t in tasks if t], return_exceptions=True)
