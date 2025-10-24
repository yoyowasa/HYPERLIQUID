# tests/unit/test_pfpl_init.py
import importlib
from asyncio import Semaphore
from decimal import Decimal
from pathlib import Path
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
    # Clear PFPLStrategy handler registry
    PFPLStrategy._FILE_HANDLERS.clear()
    module_logger = strategy_module.logger
    # New path: logs/pfpl/<SYMBOL>.csv
    new_path = (Path("logs") / "pfpl" / f"{symbol}.csv").resolve()
    removed = False
    for handler in list(module_logger.handlers):
        base = getattr(handler, "baseFilename", "")
        if base == str(new_path):
            module_logger.removeHandler(handler)
            handler.close()
            removed = True
    # Legacy path: strategy_<SYMBOL>.csv
    legacy = Path(f"strategy_{symbol}.csv")
    if not removed:
        for handler in list(module_logger.handlers):
            base = getattr(handler, "baseFilename", "")
            if base.endswith(str(legacy)):
                module_logger.removeHandler(handler)
                handler.close()
    if new_path.exists():
        new_path.unlink()
    if legacy.exists():
        legacy.unlink()


def test_init_adds_file_handler_once(monkeypatch):
    _set_credentials(monkeypatch, "HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY")
    PFPLStrategy._FILE_HANDLERS.clear()
    _remove_strategy_handler()

    sem = Semaphore(1)
    module_logger = strategy_module.logger
    symbol = "ETH-PERP"
    symbol_log = (Path("logs") / "pfpl" / f"{symbol}.csv").resolve()

    def _count_handlers() -> int:
        return sum(
            1
            for handler in module_logger.handlers
            if isinstance(handler, logging.handlers.TimedRotatingFileHandler)
            and getattr(handler, "baseFilename", "") == str(symbol_log)
        )

    first_strategy = None
    try:
        before = _count_handlers()
        first_strategy = PFPLStrategy(config={}, semaphore=sem)
        after_first = _count_handlers()
        PFPLStrategy(config={}, semaphore=sem)
        after_second = _count_handlers()
    finally:
        cleanup_symbol = first_strategy.symbol if first_strategy else symbol
        _remove_strategy_handler(cleanup_symbol)
        PFPLStrategy._FILE_HANDLERS.clear()

    assert after_first == after_second == before + 1


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


def test_caplog_keeps_symbol_file_logging(monkeypatch, caplog):
    _set_credentials(monkeypatch, "HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY")

    PFPLStrategy._FILE_HANDLERS.clear()
    _remove_strategy_handler()

    strategy = None
    try:
        with caplog.at_level(logging.INFO, logger=strategy_module.logger.name):
            strategy = PFPLStrategy(config={}, semaphore=Semaphore(1))
            log_message = "caplog-symbol-file-check"
            strategy_module.logger.info(log_message)

        assert strategy is not None

        log_path = (Path("logs") / "pfpl" / f"{strategy.symbol}.csv").resolve()
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert log_message in content
        assert log_message in caplog.text
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


def test_funding_guard_config_applied(monkeypatch):
    _set_credentials(monkeypatch, "HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY")

    monkeypatch.setattr(strategy_module.yaml, "safe_load", lambda raw_conf: {})
    PFPLStrategy._FILE_HANDLERS.clear()
    _remove_strategy_handler()

    strategy = None
    strategy_disabled = None
    now = 1_700_000_000.0

    try:
        strategy = PFPLStrategy(
            config={
                "funding_guard": {
                    "enabled": True,
                    "buffer_sec": 60,
                    "reenter_sec": 15,
                }
            },
            semaphore=Semaphore(1),
        )

        assert strategy.funding_guard_enabled is True
        assert strategy.funding_guard_buffer_sec == 60
        assert strategy.funding_guard_reenter_sec == 15
        assert strategy.funding_close_buffer_secs == 60

        strategy.next_funding_ts = now + 30
        monkeypatch.setattr(strategy_module.time, "time", lambda: now)
        assert strategy._check_funding_window() is False
        assert strategy._funding_pause is True

        monkeypatch.setattr(strategy_module.time, "time", lambda: now + 61)
        assert strategy._check_funding_window() is True
        assert strategy._funding_pause is False

        assert strategy._should_close_before_funding(now) is True

        strategy_disabled = PFPLStrategy(
            config={
                "funding_guard": {
                    "enabled": False,
                    "buffer_sec": 45,
                    "reenter_sec": 10,
                }
            },
            semaphore=Semaphore(1),
        )

        strategy_disabled.next_funding_ts = now + 5
        monkeypatch.setattr(strategy_module.time, "time", lambda: now)

        assert strategy_disabled._check_funding_window() is True
        assert strategy_disabled._should_close_before_funding(now) is False
    finally:
        if strategy is not None:
            _remove_strategy_handler(strategy.symbol)
        if strategy_disabled is not None:
            _remove_strategy_handler(strategy_disabled.symbol)
        PFPLStrategy._FILE_HANDLERS.clear()


def test_funding_guard_string_config_handled(monkeypatch):
    _set_credentials(monkeypatch, "HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY")

    monkeypatch.setattr(strategy_module.yaml, "safe_load", lambda raw_conf: {})
    PFPLStrategy._FILE_HANDLERS.clear()
    _remove_strategy_handler()

    strategy = None
    now = 1_700_000_000.0

    try:
        strategy = PFPLStrategy(
            config={
                "funding_guard": {
                    "enabled": "false",
                    "buffer_sec": "90",
                    "reenter_sec": "45",
                }
            },
            semaphore=Semaphore(1),
        )

        assert strategy.funding_guard_enabled is False
        assert strategy.funding_guard_buffer_sec == 90
        assert strategy.funding_guard_reenter_sec == 45
        assert strategy.funding_close_buffer_secs == 90

        strategy._funding_pause = True
        strategy.next_funding_ts = now + 5
        monkeypatch.setattr(strategy_module.time, "time", lambda: now)

        assert strategy._check_funding_window() is True
        assert strategy._funding_pause is False
        assert strategy._should_close_before_funding(now) is False
    finally:
        if strategy is not None:
            _remove_strategy_handler(strategy.symbol)
        PFPLStrategy._FILE_HANDLERS.clear()
