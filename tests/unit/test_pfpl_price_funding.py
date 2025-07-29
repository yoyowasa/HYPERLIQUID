import asyncio
import pytest
from bots.pfpl.strategy import PFPLStrategy
import os

# --- Dummy API keys for unit tests (valid hex strings) ---
os.environ.setdefault("HL_ACCOUNT_ADDR", "0" * 40)  # 40 hex chars → 20-byte address
os.environ.setdefault("HL_API_SECRET", "0" * 64)  # 64 hex chars → 32-byte secret


def _make_strategy(**cfg) -> PFPLStrategy:
    """
    Utility: 最小限の設定で PFPLStrategy インスタンスを生成。
    非同期 I/O は呼ばないので HTTPClient などは初期化されなくても OK。
    """
    # 既存 __init__(config: dict, semaphore: asyncio.Semaphore | None)
    return PFPLStrategy(config=cfg, semaphore=asyncio.Semaphore(1))


def test_price_with_offset():
    """eps_pct が 0.1 % のとき ±0.1 % で価格が補正されるか"""
    strategy = _make_strategy(eps_pct=0.001)  # 0.1 %
    base_px = 100.0

    buy_px = strategy._price_with_offset(base_px, "BUY")
    sell_px = strategy._price_with_offset(base_px, "SELL")

    assert buy_px == pytest.approx(99.9)  # 100 * (1 - 0.001)
    assert sell_px == pytest.approx(100.1)  # 100 * (1 + 0.001)


def test_should_close_before_funding():
    """Funding 残秒数がバッファ未満なら True、以上なら False"""
    buffer_secs = 120
    strategy = _make_strategy(funding_close_buffer_secs=buffer_secs)

    now_ts = 1_700_000_000  # ダミーの現在時刻
    # 残 100 秒 (< 120) なので True
    strategy.next_funding_ts = now_ts + 100
    assert strategy._should_close_before_funding(now_ts) is True

    # 残 200 秒 (> 120) なので False
    strategy.next_funding_ts = now_ts + 200
    assert strategy._should_close_before_funding(now_ts) is False
