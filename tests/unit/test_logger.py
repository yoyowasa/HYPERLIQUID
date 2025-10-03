import logging
import logging.handlers
from collections.abc import Iterable

import pytest

from hl_core.utils import logger as logger_module
from hl_core.utils.logger import _ColorFormatter, _NOISY_NETWORK_LOGGERS, setup_logger


def _find_handler(handlers: Iterable[logging.Handler], predicate) -> logging.Handler:
    for handler in handlers:
        if predicate(handler):
            return handler
    raise AssertionError("Expected handler not found")


@pytest.fixture(autouse=True)
def reset_logging_state():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    original_converter = logging.Formatter.converter
    original_noisy_levels = {
        name: logging.getLogger(name).level for name in _NOISY_NETWORK_LOGGERS
    }
    original_configured = logger_module._LOGGER_CONFIGURED

    logger_module._LOGGER_CONFIGURED = False

    for handler in original_handlers:
        root.removeHandler(handler)

    yield

    # Remove handlers that setup_logger added during the test
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    # Restore original handlers and state
    for handler in original_handlers:
        root.addHandler(handler)

    root.setLevel(original_level)
    logging.Formatter.converter = original_converter

    for name, level in original_noisy_levels.items():
        logging.getLogger(name).setLevel(level)

    logger_module._LOGGER_CONFIGURED = original_configured


def test_setup_logger_uses_env_for_handler_levels(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    setup_logger(bot_name="unit", log_root=tmp_path)

    root = logging.getLogger()
    assert root.level == logging.WARNING

    console_handler = _find_handler(
        root.handlers,
        lambda h: isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and isinstance(getattr(h, "formatter", None), _ColorFormatter),
    )
    file_handler = _find_handler(
        root.handlers,
        lambda h: isinstance(h, logging.handlers.TimedRotatingFileHandler),
    )

    assert console_handler.level == logging.WARNING
    assert file_handler.level == logging.WARNING

    for name in _NOISY_NETWORK_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


def test_setup_logger_honours_manual_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    setup_logger(
        bot_name="unit",
        log_root=tmp_path,
        console_level="DEBUG",
        file_level="ERROR",
    )

    root = logging.getLogger()
    assert root.level == logging.DEBUG

    console_handler = _find_handler(
        root.handlers,
        lambda h: isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and isinstance(getattr(h, "formatter", None), _ColorFormatter),
    )
    file_handler = _find_handler(
        root.handlers,
        lambda h: isinstance(h, logging.handlers.TimedRotatingFileHandler),
    )

    assert console_handler.level == logging.DEBUG
    assert file_handler.level == logging.ERROR

    for name in _NOISY_NETWORK_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.INFO


def test_setup_logger_switches_rotating_handler_for_new_bot(tmp_path, monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    log_root = tmp_path / "logs"

    setup_logger(bot_name="runner", log_root=log_root)
    setup_logger(bot_name="pfpl", log_root=log_root)

    root = logging.getLogger()
    file_handlers = [
        handler
        for handler in root.handlers
        if isinstance(handler, logging.handlers.TimedRotatingFileHandler)
    ]

    assert len(file_handlers) == 1
    handler = file_handlers[0]
    expected_pfpl = str((log_root / "pfpl" / "pfpl.log").resolve())

    assert handler.baseFilename == expected_pfpl


def test_runner_logs_do_not_leak_into_other_bot_log(tmp_path, monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    log_root = tmp_path / "logs"

    setup_logger(bot_name="runner", log_root=log_root)

    runner_logger = logging.getLogger("run_bot")
    runner_logger.setLevel(logging.INFO)
    runner_logger.info("runner-before")

    setup_logger(bot_name="pfpl", log_root=log_root)

    pfpl_logger = logging.getLogger("bots.pfpl.strategy")
    pfpl_logger.setLevel(logging.INFO)
    pfpl_logger.propagate = False
    pfpl_logger.info("pfpl-message")

    runner_logger.info("runner-after")

    root = logging.getLogger()
    file_handlers = [
        handler
        for handler in root.handlers
        if isinstance(handler, logging.handlers.TimedRotatingFileHandler)
    ]
    assert len(file_handlers) == 1

    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.flush()

    pfpl_log = (log_root / "pfpl" / "pfpl.log").resolve()
    runner_log = (log_root / "runner" / "runner.log").resolve()

    assert pfpl_log.exists()
    assert file_handlers[0].baseFilename == str(pfpl_log)
    pfpl_contents = pfpl_log.read_text(encoding="utf-8")
    assert "pfpl-message" in pfpl_contents
    assert "runner-after" not in pfpl_contents

    if runner_log.exists():
        runner_contents = runner_log.read_text(encoding="utf-8")
        assert "runner-before" in runner_contents
