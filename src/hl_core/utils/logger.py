# src/hl_core/utils/logger.py
from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
import queue
import threading
from pathlib import Path
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


class _ColorFormatter(logging.Formatter):
    """レベルに応じて色付けして表示するコンソール用フォーマッタ."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        color = _LEVEL_COLOR.get(record.levelno, "")
        record.msg = f"{color}{record.msg}{Style.RESET_ALL if color else ''}"
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
    console_level: str | int = "INFO",
    file_level: str | int = "DEBUG",
    log_root: Path | str = "logs",
    discord_webhook: str | None = None,
    force: bool = False,
) -> None:
    """
    ロガーをシングルトンで初期化する。

    Args:
        bot_name (Optional[str]): ボット名。ログディレクトリ名に使われる。
        console_level (str | int): コンソール出力のログレベル。
        file_level (str | int): ファイル出力のログレベル。
        log_root (Path | str): ログのルートディレクトリ。
        discord_webhook (str | None): Discord Webhook URL。
        force (bool): True の場合、既存のロガー設定をクリアして再初期化する。

    ```python
    from hl_core.utils.logger import setup_logger

    setup_logger(bot_name="pfpl", discord_webhook=WEBHOOK_URL)
    logger = logging.getLogger(__name__)
    logger.info("hello")
    ```
    """
    root_logger = logging.getLogger()
    # すでに初期化済みで、強制再初期化フラグがなければ何もしない
    if root_logger.handlers and not force:
        return

    # 強制再初期化の場合、既存のハンドラをすべて削除
    if force:
        for handler in root_logger.handlers[:]:
            handler.close()
            root_logger.removeHandler(handler)

    _color_init(strip=False)  # colorama 初期化

    # ---------- パス周り ----------
    log_root = Path(log_root).resolve()
    target_dir = log_root / (bot_name or "common")
    target_dir.mkdir(parents=True, exist_ok=True)

    # ---------- ハンドラ: Console ----------
    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(_ColorFormatter(_LOG_FMT, datefmt="%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(ch)

    # ---------- ハンドラ: 日次ローテート ----------
    fh = logging.handlers.TimedRotatingFileHandler(
        target_dir / f"{bot_name or 'common'}.log",
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
        utc=True,
        delay=True,
    )
    fh.setLevel(file_level)
    fh.setFormatter(logging.Formatter(_LOG_FMT, "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(fh)

    # ---------- ハンドラ: error.log ----------
    eh = logging.FileHandler(target_dir / "error.log", encoding="utf-8")
    eh.setLevel(logging.WARNING)
    eh.setFormatter(logging.Formatter(_LOG_FMT, "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(eh)

    # ---------- ハンドラ: Discord ----------
    if discord_webhook:
        dh = DiscordHandler(discord_webhook, level=logging.ERROR)
        dh.setFormatter(logging.Formatter(_LOG_FMT, "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(dh)

    # ルートロガーのレベル
    logging.getLogger().setLevel(file_level)

    # タイムゾーンを UTC に統一
    logging.Formatter.converter = lambda *args: _dt.datetime.now(tz=_TZ).timetuple()
