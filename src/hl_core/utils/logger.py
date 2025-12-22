# src/hl_core/utils/logger.py
from __future__ import annotations

import copy
import csv
import datetime as _dt
import io
import logging
from os import getenv
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler
import logging.handlers
import os
import queue
import threading
import time as _time
from typing import Final, Optional

from colorama import Fore, Style, init as _color_init

# ────────────────────────────────────────────────────────────
# 内部定数
# ────────────────────────────────────────────────────────────
_TZ: Final = _dt.timezone(_dt.timedelta(hours=9))  # すべて UTC
_LOG_FMT: Final = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
# PIDも含めてCSV出力する
_CSV_FIELDS_WITH_LOGGER: Final = ("asctime", "levelname", "process", "name", "message")
_CSV_FIELDS_NO_LOGGER: Final = ("asctime", "levelname", "process", "message")
_LEVEL_COLOR: Final = {
    logging.DEBUG: Fore.CYAN,
    logging.INFO: Fore.GREEN,
    logging.WARNING: Fore.YELLOW,
    logging.ERROR: Fore.RED,
    logging.CRITICAL: Fore.MAGENTA,
}

_NOISY_NETWORK_LOGGERS: Final = (
    "websockets",
    "websockets.client",
    "websockets.server",
    "asyncio",
    "asyncio.base_events",
    "asyncio.selector_events",
)

_LOGGER_CONFIGURED = False


def _utc_converter(timestamp: float | None) -> _time.struct_time:
    ts = 0.0 if timestamp is None else float(timestamp)
    return _dt.datetime.fromtimestamp(ts, tz=_TZ).timetuple()


class _ColorFormatter(logging.Formatter):
    """レベルに応じて色付けして表示するコンソール用フォーマッタ."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        record = copy.copy(record)
        color = _LEVEL_COLOR.get(record.levelno, "")
        if color:
            record.msg = f"{color}{record.msg}{Style.RESET_ALL}"
        return super().format(record)


class _CsvFormatter(logging.Formatter):
    """CSV 形式でログ行を生成するフォーマッタ."""

    def __init__(
        self,
        *,
        fields: tuple[str, ...],
        datefmt: str | None = None,
    ) -> None:
        super().__init__(None, datefmt)
        self._fields = fields

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        record = copy.copy(record)
        record.message = record.getMessage()

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.stack_info:
            record.stack_info = self.formatStack(record.stack_info)

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        row: list[str] = []
        for field in self._fields:
            if field == "asctime":
                row.append(self.formatTime(record, self.datefmt))
            elif field == "message":
                row.append(record.message)
            else:
                value = getattr(record, field, "")
                row.append(str(value) if value is not None else "")
        writer.writerow(row)
        output = buffer.getvalue().rstrip("\r\n")

        if record.exc_text:
            output = f"{output}\n{record.exc_text}"
        if record.stack_info:
            output = f"{output}\n{record.stack_info}"
        return output


def create_csv_formatter(*, include_logger_name: bool = True) -> logging.Formatter:
    fields = (
        _CSV_FIELDS_WITH_LOGGER if include_logger_name else _CSV_FIELDS_NO_LOGGER
    )
    return _CsvFormatter(fields=fields, datefmt="%Y-%m-%d %H:%M:%S")


# ────────────────────────────────────────────────────────────
# Discord 送信用ハンドラ（エラー以上のみを送る想定）
# ────────────────────────────────────────────────────────────
class DiscordHandler(logging.Handler):
    """非同期キュー経由で Discord Webhook に送信するハンドラ."""

    def __init__(self, webhook_url: str, level: int = logging.ERROR) -> None:
        super().__init__(level)
        self._webhook_url = webhook_url
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            msg = self.format(record)
            self._queue.put_nowait(msg)
        except Exception:  # pragma: no cover
            self.handleError(record)

    def _worker(self) -> None:
        import json
        import urllib.request

        while True:
            content = self._queue.get()
            data = json.dumps({"content": content}).encode()
            req = urllib.request.Request(
                self._webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=5).close()
            except Exception:
                # Discord 送信失敗時は捨てるだけで落とさない
                pass


# ────────────────────────────────────────────────────────────
# パブリック API
# ────────────────────────────────────────────────────────────
def setup_logger(
    bot_name: Optional[str] = None,
    *,
    console_level: str | int | None = None,
    file_level: str | int | None = None,
    log_root: Path | str = "logs",
    discord_webhook: str | None = None,
) -> None:
    """
    ロガーをシングルトンで初期化する。

    ```python
    from hl_core.utils.logger import setup_logger

    setup_logger(bot_name="pfpl", discord_webhook=WEBHOOK_URL)
    logger = logging.getLogger(__name__)
    logger.info("hello")
    ```

    The console and rotating file handlers default to the ``LOG_LEVEL``
    environment variable (``INFO`` when unset). Passing ``console_level`` or
    ``file_level`` overrides the respective handler regardless of the
    environment configuration.
    """

    # ---------- レベルの解決 ----------
    def _coerce_level(value: str | int | None, *, default: int) -> int:
        if value is None:
            return default
        if isinstance(value, int):
            return value

        text = str(value).strip()
        if not text:
            return default
        if text.isdecimal() or (text[0] in {"+", "-"} and text[1:].isdecimal()):
            return int(text)

        numeric = logging.getLevelName(text.upper())
        if isinstance(numeric, int):
            return numeric
        raise ValueError(f"Unknown log level: {value!r}")

    env_level = os.getenv("LOG_LEVEL")
    default_level = _coerce_level(env_level, default=logging.INFO)
    console_level_value = _coerce_level(console_level, default=default_level)
    file_level_value = _coerce_level(file_level, default=default_level)
    root_level = min(console_level_value, file_level_value)
    quiet_network_level = max(logging.INFO, root_level)

    root_logger = logging.getLogger()

    def _apply_effective_levels() -> None:
        root_logger.setLevel(root_level)
        for handler in root_logger.handlers:
            if isinstance(handler, logging.handlers.TimedRotatingFileHandler):
                handler.setLevel(file_level_value)
            elif isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(console_level_value)
        for name in _NOISY_NETWORK_LOGGERS:
            logging.getLogger(name).setLevel(quiet_network_level)

    log_root = Path(log_root).resolve()
    target_dir = log_root / (bot_name or "common")
    target_dir.mkdir(parents=True, exist_ok=True)
    rotating_log_path = target_dir / f"{bot_name or 'common'}.csv"
    error_log_path = target_dir / "error.csv"

    def _build_bot_filter() -> logging.Filter | None:
        if not bot_name:
            return None

        target = (bot_name or "").lower()

        class _BotFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
                name = (record.name or "").lower()
                if not target:
                    return True

                if target == "runner" and name in {"__main__", "run_bot", "runner"}:
                    return True

                if name == target or name.startswith(f"{target}."):
                    return True

                if name.startswith(f"bots.{target}"):
                    return True

                if name.startswith("hl_core.") or name.startswith("hyperliquid."):
                    return True

                return False

        return _BotFilter()

    def _ensure_rotating_file_handler() -> None:
        existing_handler: logging.handlers.TimedRotatingFileHandler | None = None

        def _detach_from_non_propagating(
            handler: logging.handlers.TimedRotatingFileHandler,
        ) -> None:
            attached = getattr(handler, "_hyperliquid_attached_loggers", ())
            if not attached:
                return

            for logger_name in tuple(attached):
                logger = logging.getLogger(logger_name)
                try:
                    logger.removeHandler(handler)
                except ValueError:
                    pass
            setattr(handler, "_hyperliquid_attached_loggers", ())

        def _attach_to_non_propagating(
            handler: logging.handlers.TimedRotatingFileHandler,
            *,
            bot_filter: logging.Filter | None,
            target_path: Path,
        ) -> None:
            if not bot_filter:
                setattr(handler, "_hyperliquid_attached_loggers", ())
                return

            attached: tuple[str, ...] = getattr(
                handler, "_hyperliquid_attached_loggers", ()
            )
            if attached:
                _detach_from_non_propagating(handler)

            matched: list[str] = []
            manager = logging.Logger.manager
            for name, candidate in manager.loggerDict.items():
                if not isinstance(candidate, logging.Logger):
                    continue

                logger = candidate
                if logger.propagate:
                    continue

                if any(
                    getattr(existing, "baseFilename", None) == str(target_path)
                    for existing in logger.handlers
                ):
                    continue

                probe = logging.LogRecord(
                    name=name,
                    level=logging.INFO,
                    pathname="",
                    lineno=0,
                    msg="",
                    args=(),
                    exc_info=None,
                )
                if not bot_filter.filter(probe):
                    continue

                logger.addHandler(handler)
                matched.append(name)

            setattr(handler, "_hyperliquid_attached_loggers", tuple(matched))

        for handler in list(root_logger.handlers):
            if not isinstance(handler, logging.handlers.TimedRotatingFileHandler):
                continue

            if getattr(handler, "baseFilename", None) == str(rotating_log_path):
                existing_handler = handler
                continue

            _detach_from_non_propagating(handler)
            root_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

        bot_filter = _build_bot_filter()

        if existing_handler is not None:
            existing_handler.setLevel(file_level_value)
            existing_handler.setFormatter(create_csv_formatter())
            existing_handler.filters[:] = []
            if bot_filter:
                existing_handler.addFilter(bot_filter)
            _attach_to_non_propagating(
                existing_handler,
                bot_filter=bot_filter,
                target_path=rotating_log_path,
            )
            return

        fh = logging.handlers.TimedRotatingFileHandler(
            filename=str(rotating_log_path),
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
            utc=False,
        )
        fh.setLevel(file_level_value)
        fh.setFormatter(create_csv_formatter())
        if bot_filter:
            fh.addFilter(bot_filter)
        root_logger.addHandler(fh)
        _attach_to_non_propagating(
            fh,
            bot_filter=bot_filter,
            target_path=rotating_log_path,
        )

    def _ensure_error_file_handler() -> None:
        for handler in root_logger.handlers:
            if (
                isinstance(handler, logging.FileHandler)
                and not isinstance(handler, logging.handlers.TimedRotatingFileHandler)
                and getattr(handler, "baseFilename", None) == str(error_log_path)
            ):
                return

        eh = logging.FileHandler(str(error_log_path), encoding="utf-8")
        eh.setLevel(logging.WARNING)
        eh.setFormatter(create_csv_formatter())
        root_logger.addHandler(eh)

    global _LOGGER_CONFIGURED
    if not _LOGGER_CONFIGURED:
        _color_init(strip=False)  # colorama 初期化

        # ---------- ハンドラ: Console ----------
        ch = logging.StreamHandler()
        ch.setLevel(console_level_value)
        ch.setFormatter(_ColorFormatter(_LOG_FMT, datefmt="%Y-%m-%d %H:%M:%S"))
        root_logger.addHandler(ch)

        _ensure_rotating_file_handler()
        _ensure_error_file_handler()

        # ---------- ハンドラ: Discord ----------
        if discord_webhook:
            dh = DiscordHandler(discord_webhook, level=logging.ERROR)
            dh.setFormatter(logging.Formatter(_LOG_FMT, "%Y-%m-%d %H:%M:%S"))
            root_logger.addHandler(dh)

        # ルートロガーと関連ロガーのレベル
        _apply_effective_levels()

        # タイムゾーンを UTC に統一
        logging.Formatter.converter = staticmethod(_utc_converter)  # type: ignore[assignment]
        _LOGGER_CONFIGURED = True
        return

    _ensure_rotating_file_handler()
    _ensure_error_file_handler()
    _apply_effective_levels()


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a logger with daily rotating file handler attached."""

    logger = logging.getLogger(name)
    if name is None:
        name = logger.name
    _attach_daily_file_handler(logger, name)
    # 役割: 戦略ロガー(bots.*)が root に伝播して runner.csv 等へ二重出力されるのを防ぐ
    if name.startswith("bots."):
        logger.propagate = False
    return logger


def _resolve_log_path(logger_name: str) -> Path:
    """
    logger名から logs/<bot>/<bot>.csv を返す。
    'pfpl'や'pfplstrategy'を含む場合は logs/pfpl/pfpl.csv に正規化する。
    ディレクトリが無ければ作成する。
    """

    raw = logger_name.split(".")[-1].lower()
    bot = "pfpl" if ("pfpl" in raw or "pfplstrategy" in raw) else raw
    log_dir = Path("logs") / bot
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{bot}.csv"


def _attach_daily_file_handler(logger: logging.Logger, logger_name: str) -> None:
    """
    - 既に同じファイルに出すハンドラがあれば何もしない
    - UTF-8で深夜ローテ、14世代保持
    - 直ちにファイルを作成(delay=False)
    """

    path = _resolve_log_path(logger_name)
    if path.name == "strategy.csv" and getenv("HL_ENABLE_STRATEGY_GLOBAL_LOG", "0") != "1":
        # 役割: strategy.csv の出力を環境変数で制御し、既定では生成しない
        return
    for handler in logger.handlers:
        if getattr(handler, "baseFilename", None) == str(path):
            return

    file_handler = TimedRotatingFileHandler(
        filename=str(path),
        when="midnight",
        backupCount=14,
        encoding="utf-8",
        utc=False,
        delay=False,
    )
    file_handler.setLevel(logger.level)
    file_handler.setFormatter(create_csv_formatter())
    logger.addHandler(file_handler)
    logger.propagate = False
