# tests/unit/test_pfpl_on_message.py
from asyncio import Semaphore
from decimal import Decimal
import logging

import pytest

from bots.pfpl import PFPLStrategy


@pytest.fixture
def strategy(monkeypatch: pytest.MonkeyPatch) -> PFPLStrategy:
    monkeypatch.setenv("HL_ACCOUNT_ADDR", "0xTEST")
    monkeypatch.setenv("HL_API_SECRET", "0x" + "11" * 32)
    return PFPLStrategy(config={}, semaphore=Semaphore(1))


def _message(mid_key: str, value: str) -> dict[str, object]:
    return {"channel": "allMids", "data": {"mids": {mid_key: value}}}


def test_on_message_updates_mid_from_base_coin(strategy: PFPLStrategy, caplog):
    caplog.set_level(logging.DEBUG, logger="bots.pfpl.strategy")
    caplog.clear()

    strategy.on_message(_message("ETH", "123.45"))

    assert strategy.mid == Decimal("123.45")
    assert any(
        record.levelno == logging.DEBUG
        and record.message == "allMids: mid[ETH]=123.45"
        for record in caplog.records
    )


def test_on_message_missing_mid_keeps_previous(strategy: PFPLStrategy, caplog):
    caplog.set_level(logging.DEBUG, logger="bots.pfpl.strategy")
    caplog.clear()

    strategy.mid = Decimal("77.7")
    strategy.on_message(_message("BTC", "30000"))

    assert strategy.mid == Decimal("77.7")
    assert any(
        record.levelno == logging.DEBUG
        and record.message == "allMids: waiting for mid for ETH-PERP (base=ETH)"
        for record in caplog.records
    )
