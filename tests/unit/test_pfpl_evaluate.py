# tests/unit/test_pfpl_evaluate.py
import asyncio
from asyncio import Semaphore
import contextlib
from decimal import Decimal, ROUND_DOWN
import logging
import threading
import time
from typing import Iterator
from types import SimpleNamespace

import anyio
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
            f"strategy_{strat.symbol}.csv"
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


@pytest.mark.asyncio
async def test_place_order_runs_in_thread_pool(
    strategy: PFPLStrategy, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    calls: list[dict[str, object]] = []

    def blocking_order(**kwargs):
        started.set()
        time.sleep(0.3)
        calls.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(strategy.exchange, "order", blocking_order, raising=False)

    strategy.qty_tick = Decimal("0.001")

    order_task = asyncio.create_task(
        strategy.place_order("BUY", 0.01234, order_type="market")
    )

    for _ in range(50):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    else:
        order_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await order_task
        pytest.fail("order thread did not start in time")

    t0 = time.perf_counter()
    await asyncio.sleep(0.05)
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.2, "event loop should not be blocked by blocking order"
    assert not order_task.done(), "order should still be running while sleep completes"

    await order_task

    assert calls and calls[0]["sz"] == pytest.approx(0.012)


@pytest.mark.asyncio
async def test_place_order_retry_waits_between_attempts(
    strategy: PFPLStrategy, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def flaky_order(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise RuntimeError("boom")
        return {"status": "ok"}

    monkeypatch.setattr(strategy.exchange, "order", flaky_order, raising=False)

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float, *args, **kwargs) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)

    strategy.qty_tick = Decimal("0.001")

    await strategy.place_order("SELL", 0.02, order_type="market")

    assert attempts == 2
    assert sleep_delays == [0.5]
