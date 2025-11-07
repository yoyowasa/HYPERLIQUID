from __future__ import annotations
from typing import Any, Tuple

import pytest


@pytest.mark.asyncio
async def test_place_order_paper_stub(monkeypatch):
    # DRY_RUN=true で paper 応答になること
    monkeypatch.setenv("DRY_RUN", "true")
    from hl_core.api.http import place_order

    resp = await place_order(
        symbol="BTCUSD-PERP",
        side="BUY",
        size=0.01,
        price=70000.0,
        time_in_force="GTC",
        paper=True,
    )
    assert resp.get("status") == "paper"
    assert str(resp.get("order_id", "")).startswith("paper-")


@pytest.mark.asyncio
async def test_place_order_live_gate_blocks_without_go(monkeypatch):
    # Live でも GO 未設定は拒否
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "0xabc")
    monkeypatch.setenv("HL_PRIVATE_KEY", "0x" + "11" * 32)
    from hl_core.api.http import place_order

    with pytest.raises(ValueError):
        await place_order(
            symbol="BTCUSD-PERP",
            side="SELL",
            size=0.02,
            price=69000.0,
            time_in_force="GTC",
            paper=False,
        )


@pytest.mark.asyncio
async def test_place_order_live_calls_exchange_order(monkeypatch):
    # GO=live + 認証あり で Exchange.order が呼ばれること
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("GO", "live")
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "0xabc")
    monkeypatch.setenv("HL_PRIVATE_KEY", "0x" + "22" * 32)

    # make_clients をスタブして Exchange.order 呼び出しを捕捉
    class _SpyExchange:
        def __init__(self) -> None:
            self.last_args: Tuple[tuple, dict] | None = None

        def order(self, *args: Any, **kwargs: Any):  # 同期関数を想定
            self.last_args = (args, kwargs)
            return {"response": {"data": {"statuses": [{"oid": 12345}]}}}

    spy = _SpyExchange()

    def _fake_make_clients(_settings):
        return object(), spy, "0xabc"

    import hl_core.api.http as http_mod
    import hl_core.hl_client as hlc_mod

    # Live 実装は関数内で hl_core.hl_client から import するため、そちらを差し替える
    monkeypatch.setattr(hlc_mod, "make_clients", _fake_make_clients, raising=True)

    resp = await http_mod.place_order(
        symbol="ETH-PERP",
        side="BUY",
        size=0.05,
        price=3000.0,
        time_in_force="GTT",
        ttl_s=1.0,
        display_size=0.01,
        iceberg=True,
        post_only=True,
        reduce_only=False,
        paper=False,
    )

    # 戻りの order_id は抽出できていること
    assert resp.get("status") == "live"
    assert resp.get("order_id") == "12345"

    # SDK へ渡す kwargs の要点を検証（coin/is_buy/sz/limit_px/tif）
    assert spy.last_args is not None
    _args, kwargs = spy.last_args
    assert kwargs.get("coin") == "ETH"
    assert kwargs.get("is_buy") is True
    assert kwargs.get("sz") == pytest.approx(0.05)
    assert kwargs.get("limit_px") == pytest.approx(3000.0)
    assert kwargs.get("tif") == "GTT"
