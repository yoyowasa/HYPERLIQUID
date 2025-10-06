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
        record.levelno == logging.DEBUG and record.message == "allMids: mid[ETH]=123.45"
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


def test_on_message_fair_updates_when_symbol_only_index(strategy: PFPLStrategy):
    strategy.mid = Decimal("1000")
    strategy.fair_feed = "indexPrices"
    calls: list[Decimal | None] = []

    def fake_evaluate(self: PFPLStrategy) -> None:
        calls.append(self.fair)

    strategy.evaluate = fake_evaluate.__get__(strategy, PFPLStrategy)

    strategy.on_message(
        {
            "channel": "indexPrices",
            "data": {"prices": {strategy.symbol: "1234.56"}},
        }
    )

    assert strategy.idx == Decimal("1234.56")
    assert strategy.fair == Decimal("1234.56")
    assert calls == [Decimal("1234.56")]


def test_on_message_fair_updates_when_symbol_only_oracle(strategy: PFPLStrategy):
    strategy.mid = Decimal("2000")
    strategy.fair_feed = "oraclePrices"
    calls: list[Decimal | None] = []

    def fake_evaluate(self: PFPLStrategy) -> None:
        calls.append(self.fair)

    strategy.evaluate = fake_evaluate.__get__(strategy, PFPLStrategy)

    strategy.on_message(
        {
            "channel": "oraclePrices",
            "data": {"prices": {strategy.symbol: "4321.09"}},
        }
    )

    assert strategy.ora == Decimal("4321.09")
    assert strategy.fair == Decimal("4321.09")
    assert calls == [Decimal("4321.09")]
