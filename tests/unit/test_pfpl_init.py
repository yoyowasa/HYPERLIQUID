# tests/unit/test_pfpl_init.py
from asyncio import Semaphore
from decimal import Decimal
import logging
import pytest
from bots.pfpl import PFPLStrategy

TEST_ACCOUNT = "0xTEST"
TEST_KEY = "0x" + "11" * 32


def _set_credentials(
    monkeypatch: pytest.MonkeyPatch, account_key: str, secret_key: str
) -> None:
    for var in (
        "HL_ACCOUNT_ADDRESS",
        "HL_ACCOUNT_ADDR",
        "HL_PRIVATE_KEY",
        "HL_API_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(account_key, TEST_ACCOUNT)
    monkeypatch.setenv(secret_key, TEST_KEY)


def _remove_strategy_handler(symbol: str = "ETH-PERP") -> None:
    PFPLStrategy._FILE_HANDLERS.discard(symbol)
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if isinstance(handler, logging.FileHandler):
            filename = getattr(handler, "baseFilename", "")
            if filename.endswith(f"strategy_{symbol}.log"):
                root_logger.removeHandler(handler)
                handler.close()


def test_init_adds_file_handler_once(monkeypatch):
    _set_credentials(monkeypatch, "HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY")
    PFPLStrategy._FILE_HANDLERS.clear()
    _remove_strategy_handler()

    sem = Semaphore(1)

    first_strategy: PFPLStrategy | None = None
    try:
        before = len(logging.getLogger().handlers)
        first_strategy = PFPLStrategy(config={}, semaphore=sem)
        after_first = len(logging.getLogger().handlers)
        PFPLStrategy(config={}, semaphore=sem)
        after_second = len(logging.getLogger().handlers)
    finally:
        symbol = first_strategy.symbol if first_strategy else "ETH-PERP"
        _remove_strategy_handler(symbol)
        PFPLStrategy._FILE_HANDLERS.clear()

    assert after_first == after_second > before


@pytest.mark.parametrize(
    ("account_env", "secret_env"),
    [
        ("HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY"),
        ("HL_ACCOUNT_ADDR", "HL_API_SECRET"),
    ],
)
def test_init_accepts_new_and_legacy_env(monkeypatch, account_env, secret_env):
    _set_credentials(monkeypatch, account_env, secret_env)

    strategy: PFPLStrategy | None = None
    try:
        strategy = PFPLStrategy(config={}, semaphore=Semaphore(1))
        assert strategy.account == TEST_ACCOUNT
        assert strategy.secret == TEST_KEY
    finally:
        if strategy is not None:
            _remove_strategy_handler(strategy.symbol)
        PFPLStrategy._FILE_HANDLERS.clear()


def test_init_missing_credentials_raises(monkeypatch):
    for var in (
        "HL_ACCOUNT_ADDRESS",
        "HL_ACCOUNT_ADDR",
        "HL_PRIVATE_KEY",
        "HL_API_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ValueError) as excinfo:
        PFPLStrategy(config={}, semaphore=Semaphore(1))

    msg = str(excinfo.value)
    assert "HL_PRIVATE_KEY" in msg and "HL_API_SECRET" in msg


@pytest.mark.asyncio
async def test_refresh_position_uses_base_coin(monkeypatch):
    _set_credentials(monkeypatch, "HL_ACCOUNT_ADDR", "HL_API_SECRET")

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

    try:
        await strategy._refresh_position()

        assert strategy.pos_usd == Decimal("6")
    finally:
        _remove_strategy_handler(strategy.symbol)
        PFPLStrategy._FILE_HANDLERS.clear()
