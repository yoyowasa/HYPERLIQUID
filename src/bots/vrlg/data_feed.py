from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import Optional, Protocol, runtime_checkable

from hl_core.utils.logger import get_logger

logger = get_logger("VRLG.data_feed")


@runtime_checkable
class L1Like(Protocol):
    """〔このプロトコルがすること〕 L1相当の最良気配データ型を表します。"""

    best_bid: float
    best_ask: float
    bid_size_l1: float
    ask_size_l1: float


@dataclass(frozen=True)
class FeatureSnapshot:
    """
    〔このデータクラスがすること〕
    100msごとの特徴量スナップショットを表します。
    t: 取得時刻（秒, time.time()）
    mid: (bestBid + bestAsk) / 2
    spread_ticks: (bestAsk - bestBid) / tick_size
    dob: bidSize_L1 + askSize_L1
    obi: (bidSize_L1 - askSize_L1) / max(dob, 1e-9)
    block_phase: RotationDetector が付与する位相（0〜1）
    """

    t: float
    mid: float
    spread_ticks: float
    dob: float
    obi: float
    block_phase: Optional[float] = None

    def with_phase(self, phase: float) -> "FeatureSnapshot":
        """〔このメソッドがすること〕 block_phase を埋めた新インスタンスを返します。"""

        return replace(self, block_phase=phase)


def _tick_size_from_cfg(cfg) -> float:
    """〔この関数がすること〕 設定オブジェクトから tick_size を安全に取り出します。"""

    try:
        return float(getattr(cfg.symbol, "tick_size"))
    except Exception:
        try:
            return float(cfg["symbol"]["tick_size"])  # type: ignore[index]
        except Exception as e:  # pragma: no cover
            raise ValueError("tick_size not found in config") from e


def compute_features(book: L1Like, tick_size: float, ts: Optional[float] = None) -> FeatureSnapshot:
    """
    〔この関数がすること〕
    L1 の最良気配から mid / spread_ticks / DoB / OBI を計算し、FeatureSnapshot を返します。
    """

    t = time.time() if ts is None else ts
    mid = (book.best_bid + book.best_ask) / 2.0
    spread_ticks = (book.best_ask - book.best_bid) / max(tick_size, 1e-12)
    dob = float(book.bid_size_l1) + float(book.ask_size_l1)
    obi = 0.0
    if dob > 0:
        obi = (float(book.bid_size_l1) - float(book.ask_size_l1)) / dob
    return FeatureSnapshot(t=t, mid=mid, spread_ticks=spread_ticks, dob=dob, obi=obi)


async def run_feeds(cfg, q_features: asyncio.Queue) -> None:
    """
    〔この関数がすること〕
    - （後で実装する）WS購読タスクを起動し、最新の L1 を保持する
    - 100ms ごとに最新L1から FeatureSnapshot を作って q_features に入れる
    - hl_core.api.ws が未実装の場合は待機（ログを出す）
    """

    tick_size = _tick_size_from_cfg(cfg)
    last_book: Optional[L1Like] = None
    stop = asyncio.Event()

    async def _consume_level2() -> None:
        """〔この内部関数がすること〕 level2 の WS を購読して last_book を更新します。"""

        nonlocal last_book
        try:
            from hl_core.api.ws import subscribe_level2  # type: ignore
        except Exception as e:
            logger.warning("WS adapter not available yet: %s; waiting…", e)
            await stop.wait()
            return

        symbol = getattr(cfg.symbol, "name", None) or (cfg["symbol"]["name"])  # type: ignore[index]
        async for book in subscribe_level2(symbol):
            last_book = book

    async def _feature_clock() -> None:
        """〔この内部関数がすること〕 100ms 間隔で FeatureSnapshot を生成しキューへ投入します。"""

        interval = 0.1
        while not stop.is_set():
            started = time.perf_counter()
            if last_book is not None:
                snap = compute_features(last_book, tick_size)
                try:
                    q_features.put_nowait(snap)
                except asyncio.QueueFull:
                    _ = q_features.get_nowait()
                    await q_features.put(snap)
            elapsed = time.perf_counter() - started
            await asyncio.sleep(max(0.0, interval - elapsed))

    tasks = [
        asyncio.create_task(_consume_level2(), name="vrlg.level2"),
        asyncio.create_task(_feature_clock(), name="vrlg.feature_clock"),
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
