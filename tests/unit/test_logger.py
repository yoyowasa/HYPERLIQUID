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
