#!/usr/bin/env python
import argparse
import asyncio
import logging
from importlib import import_module
from asyncio import create_task, wait_for, Event
from dotenv import load_dotenv
from hl_core.utils.logger import setup_logger

# env
load_dotenv()

# logger
setup_logger("runner")
logger = logging.getLogger(__name__)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bot", help="bot folder name (e.g., pfpl)")
    parser.add_argument("--testnet", action="store_true", help="use testnet URL")
    parser.add_argument("--cooldown", type=float, default=1.0, help="cooldown sec")
    args = parser.parse_args()

    # dynamic import
    mod = import_module(f"bots.{args.bot}.strategy")
    strategy_cls = getattr(mod, "PFPLStrategy")
    strategy = strategy_cls(
        config={"testnet": args.testnet, "cooldown_sec": args.cooldown}
    )
    from hl_core.api import WSClient

    ws = WSClient("wss://api.hyperliquid.xyz/ws", reconnect=True)
    ws.on_message = strategy.on_message

    # ① 受信ループをバックグラウンドで開始
    connect_task = create_task(ws.connect())

    # ② 最大 5 秒だけ接続完了を待つ
    try:
        await wait_for(connect_task, timeout=5)
    except asyncio.TimeoutError:
        # まだ接続中でも OK。以降で subscribe する
        pass

    # ③ 接続が確立した（または確立途中）の状態で購読送信
    await ws.subscribe("allMids")

    # ④ 常駐
    try:
        await Event().wait()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
