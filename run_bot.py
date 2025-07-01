#!/usr/bin/env python
import argparse
import asyncio
import logging
from importlib import import_module

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
    await ws.subscribe("allMids")
    from hl_core.api import WSClient

    ws = WSClient("wss://api.hyperliquid.xyz/ws", reconnect=True)
    ws.on_message = strategy.on_message

    try:
        await ws.connect()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
