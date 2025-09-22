# tests/unit/test_pfpl_evaluate.py
import asyncio
from asyncio import Semaphore
from decimal import Decimal, ROUND_DOWN
import logging
from typing import Iterator
from types import SimpleNamespace

import pytest

from bots.pfpl import PFPLStrategy


@pytest.fixture
def strategy(monkeypatch: pytest.MonkeyPatch) -> Iterator[PFPLStrategy]:
    monkeypatch.setenv("HL_ACCOUNT_ADDR", "0xTEST")
    monkeypatch.setenv("HL_API_SECRET", "0x" + "11" * 32)
    strat = PFPLStrategy(config={}, semaphore=Semaphore(1))
    strat.config["threshold"] = "200"
    strat.config["threshold_pct"] = "200"
    strat.mid = Decimal("100")
    yield strat

    PFPLStrategy._FILE_HANDLERS.discard(strat.symbol)
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        filename = getattr(handler, "baseFilename", "")
        if isinstance(handler, logging.FileHandler) and filename.endswith(
            f"strategy_{strat.symbol}.log"
        ):
            root_logger.removeHandler(handler)
            handler.close()


def test_evaluate_logs_signed_diff(
    strategy: PFPLStrategy, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG, logger="bots.pfpl.strategy")
    caplog.clear()

    strategy.fair = Decimal("101")

    strategy.evaluate()

    assert any(
        record.levelno == logging.DEBUG
        and "diff=+1.000000" in record.message
        and "diff_pct=+1.000000" in record.message
        for record in caplog.records
    ), "expected positive diff log"

    caplog.clear()

    strategy.fair = Decimal("99")

    strategy.evaluate()

    assert any(
        record.levelno == logging.DEBUG
        and "diff=-1.000000" in record.message
        and "diff_pct=-1.000000" in record.message
        for record in caplog.records
    ), "expected negative diff log"


def test_evaluate_quantizes_size_with_qty_tick(
    strategy: PFPLStrategy, monkeypatch: pytest.MonkeyPatch
) -> None:
    strategy.config["threshold"] = "0"
    strategy.config["threshold_pct"] = "0"
    mid = strategy.mid
    assert mid is not None
    strategy.fair = mid + Decimal("5")
    strategy.order_usd = Decimal("25")
    strategy.min_usd = Decimal("0")
    strategy.qty_tick = Decimal("0.005")

    recorded: list[dict[str, object]] = []

    async def fake_place_order(self, side: str, size: float, **kwargs) -> None:
        recorded.append({"side": side, "size": size})

    monkeypatch.setattr(PFPLStrategy, "place_order", fake_place_order)

    def run_immediately(coro, *args, **kwargs):
        asyncio.run(coro)
        return SimpleNamespace(done=True)

    monkeypatch.setattr(asyncio, "create_task", run_immediately)

    strategy.evaluate()

    assert recorded, "evaluate should schedule an order"
    mid_after = strategy.mid
    assert mid_after is not None
    expected = (strategy.order_usd / mid_after).quantize(
        strategy.qty_tick, rounding=ROUND_DOWN
    )
    assert Decimal(str(recorded[0]["size"])) == expected


def test_evaluate_skips_when_size_rounds_to_zero(
    strategy: PFPLStrategy,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy.config["threshold"] = "0"
    strategy.config["threshold_pct"] = "0"
    mid = strategy.mid
    assert mid is not None
    strategy.fair = mid + Decimal("5")
    strategy.order_usd = Decimal("0.0005")
    strategy.min_usd = Decimal("0")
    strategy.qty_tick = Decimal("0.001")

    called = False

    def fail_create_task(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("create_task should not be invoked when size is zero")

    monkeypatch.setattr(asyncio, "create_task", fail_create_task)

    caplog.set_level(logging.DEBUG, logger="bots.pfpl.strategy")
    strategy.evaluate()

    assert called is False
    assert any("quantized to zero" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_place_order_quantizes_size(
    strategy: PFPLStrategy, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def fake_order(**kwargs):
        calls.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(strategy.exchange, "order", fake_order, raising=False)

    strategy.qty_tick = Decimal("0.001")
    await strategy.place_order("BUY", 0.01234, order_type="market")

    assert calls, "order should be sent"
    assert calls[0]["sz"] == pytest.approx(0.012)


@pytest.mark.asyncio
async def test_place_order_skips_zero_after_rounding(
    strategy: PFPLStrategy, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_order(**_kwargs):
        nonlocal called
        called = True
        return {"status": "ok"}

    monkeypatch.setattr(strategy.exchange, "order", fake_order, raising=False)

    strategy.qty_tick = Decimal("0.01")
    await strategy.place_order("BUY", 0.004, order_type="market")

    assert called is False
