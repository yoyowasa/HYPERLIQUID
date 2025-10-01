# tests/unit/test_pfpl_init.py
import importlib
from asyncio import Semaphore
from decimal import Decimal
import logging
import sys

import pytest
from bots.pfpl import PFPLStrategy
import bots.pfpl.strategy as strategy_module

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

    first_strategy = None
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


def test_yaml_config_overrides_cli_args(monkeypatch):
    _set_credentials(monkeypatch, "HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY")

    yaml_override = {"target_symbol": "ETH-PERP", "dry_run": False}
    monkeypatch.setattr(
        strategy_module.yaml,
        "safe_load",
        lambda raw_conf: yaml_override.copy(),
    )

    cli_symbol = "BTC-PERP"
    cli_dry_run = True

    strategy = None
    try:
        strategy = PFPLStrategy(
            config={"target_symbol": cli_symbol, "dry_run": cli_dry_run},
            semaphore=Semaphore(1),
        )

        assert strategy is not None

        assert strategy.config.get("target_symbol") == yaml_override["target_symbol"]
        assert strategy.symbol == yaml_override["target_symbol"]
        assert strategy.config.get("dry_run") == yaml_override["dry_run"]
        assert strategy.config.get("target_symbol") != cli_symbol
        assert strategy.config.get("dry_run") != cli_dry_run
    finally:
        if strategy is not None:
            _remove_strategy_handler(strategy.symbol)
        PFPLStrategy._FILE_HANDLERS.clear()


def test_init_loads_yaml_config(monkeypatch):
    _set_credentials(monkeypatch, "HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY")

    strategy = None
    try:
        PFPLStrategy._FILE_HANDLERS.clear()
        _remove_strategy_handler()

        strategy = PFPLStrategy(config={}, semaphore=Semaphore(1))

        assert strategy is not None

        assert strategy.config.get("order_usd") == 10
    finally:
        if strategy is not None:
            _remove_strategy_handler(strategy.symbol)
        PFPLStrategy._FILE_HANDLERS.clear()


def test_strategy_import_requires_pyyaml(monkeypatch):
    module_name = "bots.pfpl.strategy"
    package_name = "bots.pfpl"

    try:
        original_yaml_module = importlib.import_module("yaml")
    except ModuleNotFoundError:
        original_yaml_module = None

    sys.modules.pop(module_name, None)
    sys.modules.pop(package_name, None)
    if original_yaml_module is not None:
        sys.modules["yaml"] = original_yaml_module
    else:
        sys.modules.pop("yaml", None)
    monkeypatch.setitem(sys.modules, "yaml", None)

    restored_strategy_module = None
    restored_package_module = None
    try:
        with pytest.raises(RuntimeError) as excinfo:
            importlib.import_module(module_name)

        assert "PyYAML" in str(excinfo.value)
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(package_name, None)
        if original_yaml_module is not None:
            sys.modules["yaml"] = original_yaml_module
        else:
            sys.modules.pop("yaml", None)

        restored_package_module = importlib.import_module(package_name)
        restored_strategy_module = importlib.import_module(module_name)

        global strategy_module, PFPLStrategy
        strategy_module = restored_strategy_module
        PFPLStrategy = restored_package_module.PFPLStrategy


@pytest.mark.parametrize(
    ("account_env", "secret_env"),
    [
        ("HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY"),
        ("HL_ACCOUNT_ADDR", "HL_API_SECRET"),
    ],
)
def test_init_accepts_new_and_legacy_env(monkeypatch, account_env, secret_env):
    _set_credentials(monkeypatch, account_env, secret_env)

    strategy = None
    try:
        strategy = PFPLStrategy(config={}, semaphore=Semaphore(1))
        assert strategy is not None
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
