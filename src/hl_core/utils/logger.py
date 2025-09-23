# src/hl_core/utils/logger.py
from __future__ import annotations

import copy
import datetime as _dt
import logging
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
_TZ: Final = _dt.timezone.utc  # すべて UTC
_LOG_FMT: Final = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
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
    rotating_log_path = target_dir / f"{bot_name or 'common'}.log"
    error_log_path = target_dir / "error.log"

    def _ensure_rotating_file_handler() -> None:
        for handler in root_logger.handlers:
            if isinstance(handler, logging.handlers.TimedRotatingFileHandler) and (
                getattr(handler, "baseFilename", None) == str(rotating_log_path)
            ):
                return

        fh = logging.handlers.TimedRotatingFileHandler(
            filename=str(rotating_log_path),
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
            utc=True,
        )
        fh.setLevel(file_level_value)
        fh.setFormatter(logging.Formatter(_LOG_FMT, "%Y-%m-%d %H:%M:%S"))
        root_logger.addHandler(fh)

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
        eh.setFormatter(logging.Formatter(_LOG_FMT, "%Y-%m-%d %H:%M:%S"))
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
    return logger


def _resolve_log_path(logger_name: str) -> Path:
    """
    logger名から logs/<bot>/<bot>.log を返す。
    'pfpl'や'pfplstrategy'を含む場合は logs/pfpl/pfpl.log に正規化する。
    ディレクトリが無ければ作成する。
    """

    raw = logger_name.split(".")[-1].lower()
    bot = "pfpl" if ("pfpl" in raw or "pfplstrategy" in raw) else raw
    log_dir = Path("logs") / bot
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{bot}.log"


def _attach_daily_file_handler(logger: logging.Logger, logger_name: str) -> None:
    """
    - 既に同じファイルに出すハンドラがあれば何もしない
    - UTF-8で深夜ローテ、14世代保持
    - 直ちにファイルを作成(delay=False)
    """

    path = _resolve_log_path(logger_name)
    for handler in logger.handlers:
        if getattr(handler, "baseFilename", None) == str(path):
            return

    file_handler = TimedRotatingFileHandler(
        filename=str(path),
        when="midnight",
        backupCount=14,
        encoding="utf-8",
        utc=True,
        delay=False,
    )
    file_handler.setLevel(logger.level)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(file_handler)
    logger.propagate = False
