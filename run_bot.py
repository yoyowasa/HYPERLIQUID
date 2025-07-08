#!/usr/bin/env python
import argparse
import asyncio
import logging
from importlib import import_module
from asyncio import Event
from dotenv import load_dotenv, find_dotenv
from hl_core.utils.logger import setup_logger
from os import getenv
import anyio

# env
load_dotenv()

# logger
setup_logger(
    "runner",
    discord_webhook=getenv("DISCORD_WEBHOOK"),  # 追記
)
logger = logging.getLogger(__name__)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bot", help="bot folder name (e.g., pfpl)")
    parser.add_argument("--testnet", action="store_true", help="use testnet URL")
    parser.add_argument("--cooldown", type=float, default=1.0, help="cooldown sec")
    parser.add_argument("--dry-run", action="store_true", help="発注せずログだけ出す")
    parser.add_argument(
        "--order_usd", type=float, default=10, help="order size in USD per trade"
    )
    parser.add_argument("--threshold", type=float, help="absolute USD threshold")
    parser.add_argument("--threshold_pct", type=float, help="percentage threshold (%)")
    parser.add_argument(
        "--mode",
        choices=["abs", "pct", "both", "either"],
        default="both",
        help="abs / pct 判定モード",
    )
    parser.add_argument(
        "--log_level", default=None, help="logger level (DEBUG / INFO / …)"
    )

    args = parser.parse_args()

    # ★ ここで .env を読み分け
    env_file = ".env.test" if args.testnet else ".env"
    load_dotenv(find_dotenv(env_file), override=False)

    # dynamic import
    mod = import_module(f"bots.{args.bot}.strategy")
    strategy_cls = getattr(mod, "PFPLStrategy")
    strategy = strategy_cls(
        config={
            "testnet": args.testnet,
            "cooldown_sec": args.cooldown,
            "dry_run": args.dry_run,
            "order_usd": args.order_usd,
            "threshold": args.threshold,
            "threshold_pct": args.threshold_pct,
            "mode": args.mode,
            "log_level": args.log_level,
        }
    )
    from hl_core.api import WSClient

    ws = WSClient("wss://api.hyperliquid.xyz/ws", reconnect=True)
    ws.on_message = strategy.on_message

    await ws.wait_ready()  # open まで待機
    await ws.subscribe("allMids")  # mid
    await ws.subscribe("indexPrices")  # ★ fair

    asyncio.create_task(ws.connect())  # 非同期で接続開始

    # --- ★ 接続完了を待つループ -----------------------------
    while not (ws._ws and not getattr(ws._ws, "closed", False)):
        await anyio.sleep(0.1)  # 0.1 秒スリープ
    # --------------------------------------------------------

    try:
        await Event().wait()  # 常駐
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
