from __future__ import annotations

import asyncio
import math
import time
from typing import Any, List, Tuple

import pytest

# 実行環境の import パス差（src 直下 or パッケージ化）に対応
try:
    from bots.vrlg.execution_engine import ExecutionEngine
except Exception:
    from src.bots.vrlg.execution_engine import ExecutionEngine  # type: ignore


class SpyExec(ExecutionEngine):
    """〔このクラスがすること〕
    ExecutionEngine を継承し、実際の発注APIを呼ばずに「渡された価格・サイド」を記録します。
    """

    def __init__(self, cfg: Any, paper: bool) -> None:
        super().__init__(cfg, paper)
        self.prices: List[Tuple[str, float]] = []

    async def _post_only_iceberg(
        self, side: str, price: float, total: float, display: float, ttl_s: float
    ) -> str | None:
        """〔このメソッドがすること〕 スパイ用に価格を保存し、ダミー order_id を返します。"""
        self.prices.append((side, float(price)))
        return f"spy-{side}-{price}"

    async def _cancel_many(self, order_ids: list[str]) -> None:
        """〔このメソッドがすること〕 キャンセルは何もしない（テストを速く安全に）。"""
        return

    async def flatten_ioc(self) -> None:
        """〔このメソッドがすること〕 IOC クローズは何もしない（プレースホルダ）。"""
        return


def _cfg() -> dict:
    """〔この関数がすること〕 テスト用の最小コンフィグ（tick=0.5, TTL短め）を返します。"""
    return {
        "symbol": {"name": "BTCUSD-PERP", "tick_size": 0.5},
        "exec": {
            "order_ttl_ms": 50,
            "display_ratio": 0.25,
            "min_display_btc": 0.01,
            "max_exposure_btc": 0.8,
            "cooldown_factor": 2.0,
        },
    }


@pytest.mark.asyncio
async def test_place_two_sided_offsets_and_rounding() -> None:
    """〔このテストがすること〕
    通常（±0.5tick）と深置き（±1.5tick）で、SELL−BUY の価格差が 0.5 / 1.5 になることを検証します。
    """
    eng = SpyExec(_cfg(), paper=True)
    mid = 70000.25  # 0.5tick 丸めの性質が分かりやすいミッド

    # 通常: ±0.5tick → 価格差は 1 tick = 0.5
    eng.prices.clear()
    ids = await eng.place_two_sided(mid=mid, total=0.05, deepen=False)
    assert len(ids) == 2, "通常置きで両面の発注が出ていません"
    prices = dict(eng.prices)  # {"BUY": px_bid, "SELL": px_ask}
    diff_normal = prices["SELL"] - prices["BUY"]
    assert math.isclose(diff_normal, 0.5, rel_tol=0, abs_tol=1e-9), f"通常置きのスプレッド期待=0.5, got={diff_normal}"

    # 深置き: ±1.5tick → 価格差は 3 tick = 1.5
    eng.prices.clear()
    ids = await eng.place_two_sided(mid=mid, total=0.05, deepen=True)
    assert len(ids) == 2, "深置きで両面の発注が出ていません"
    prices = dict(eng.prices)
    diff_deep = prices["SELL"] - prices["BUY"]
    assert math.isclose(diff_deep, 1.5, rel_tol=0, abs_tol=1e-9), f"深置きのスプレッド期待=1.5, got={diff_deep}"


@pytest.mark.asyncio
async def test_cooldown_skips_same_side() -> None:
    """〔このテストがすること〕 register_fill() 後は同方向の発注がクールダウンでスキップされることを確認します。"""
    eng = SpyExec(_cfg(), paper=True)
    mid = 70000.25

    # 直前のフィルを BUY と記録 → 直後の place_two_sided では BUY がスキップされ SELL のみ出る想定
    eng.register_fill("BUY")
    ids = await eng.place_two_sided(mid=mid, total=0.05, deepen=False)
    # BUY がスキップされ SELL のみ=1件になるはず
    assert len(ids) == 1, f"クールダウンで同方向をスキップできていません（ids={ids})"
