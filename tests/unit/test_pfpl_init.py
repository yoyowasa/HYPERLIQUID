# tests/unit/test_pfpl_init.py
from asyncio import Semaphore
from decimal import Decimal
import logging
import pytest
from bots.pfpl import PFPLStrategy


def test_init(monkeypatch):
    # ── ダミー鍵 (32 byte hex) を環境変数にセット ──
    monkeypatch.setenv("HL_ACCOUNT_ADDR", "0xTEST")
    monkeypatch.setenv("HL_API_SECRET", "0x" + "11" * 32)

    # ── セマフォは 1 で十分 ──
    sem = Semaphore(1)

    # 初回初期化でハンドラが増える
    before = len(logging.getLogger().handlers)
    PFPLStrategy(config={}, semaphore=sem)
    after_first = len(logging.getLogger().handlers)
    # 2 度目でもハンドラが増えないことを確認
    PFPLStrategy(config={}, semaphore=sem)
    after_second = len(logging.getLogger().handlers)
    assert after_first == after_second > before


@pytest.mark.asyncio
async def test_refresh_position_uses_base_coin(monkeypatch):
    monkeypatch.setenv("HL_ACCOUNT_ADDR", "0xTEST")
    monkeypatch.setenv("HL_API_SECRET", "0x" + "11" * 32)

    strategy = PFPLStrategy(config={}, semaphore=Semaphore(1))
    strategy.base_coin = "SOL"
    strategy.symbol = "SOL-PERP"

    def fake_user_state(account: str):
        return {
            "perpPositions": [
                {"position": {"coin": "ETH", "sz": "1", "entryPx": "100"}},
                {"position": {"coin": "SOL", "sz": "2", "entryPx": "3"}},
            ]
        }

    monkeypatch.setattr(strategy.exchange.info, "user_state", fake_user_state)

    await strategy._refresh_position()

    assert strategy.pos_usd == Decimal("6")
