# tests/unit/test_pfpl_evaluate.py
from asyncio import Semaphore
from decimal import Decimal
import logging

import pytest

from bots.pfpl import PFPLStrategy


@pytest.fixture
def strategy(monkeypatch: pytest.MonkeyPatch) -> PFPLStrategy:
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
