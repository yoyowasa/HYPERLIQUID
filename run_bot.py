#!/usr/bin/env python
"""
Generic Bot runner.

Usage examples:
    poetry run python run_bot.py pfpl         # PFPL Bot
    poetry run python run_bot.py opdh         # 将来の Bot
"""
import asyncio
import logging
import sys
from importlib import import_module

from dotenv import load_dotenv
from hl_core.utils.logger import setup_logger  # ← import を最上部へ移動

# ── 1. .env を読む ──────────────────────────────────────────
load_dotenv()

# ── 2. ロガー初期化 ────────────────────────────────────────
setup_logger("runner")
logger = logging.getLogger(__name__)


async def main(bot_name: str) -> None:
    # ── 3. Bot モジュールを動的 import ───────────────────────
    mod_path = f"bots.{bot_name}.strategy"
    try:
        mod = import_module(mod_path)
    except ModuleNotFoundError as exc:
        logger.error("Bot '%s' not found (%s)", bot_name, exc)
        sys.exit(1)

    # Strategy クラスを取得
    strategy_cls = getattr(mod, "PFPLStrategy", None)
    if strategy_cls is None:
        logger.error("PFPLStrategy not found in %s", mod_path)
        sys.exit(1)

    strategy = strategy_cls(config={})

    # ── 4. WS クライアントを作成し Strategy へフック ──────────
    from hl_core.api import WSClient

    ws = WSClient("wss://api.hyperliquid.xyz/ws", reconnect=True)
    ws.on_message = strategy.on_message

    # ── 5. 実行 & Ctrl-C で安全停止 ───────────────────────────
    try:
        await ws.connect()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        await ws.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python run_bot.py <bot_folder>")
        sys.exit(1)

    asyncio.run(main(sys.argv[1]))
