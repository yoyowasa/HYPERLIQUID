#!/usr/bin/env python
import argparse
import asyncio
import logging
from importlib import import_module
from os import getenv
from pathlib import Path

from dotenv import load_dotenv

from hl_core.api import WSClient
from hl_core.utils.logger import setup_logger


# env
load_dotenv()

# logger
setup_logger(
    "runner",
    discord_webhook=getenv("DISCORD_WEBHOOK"),  # 追記
)
logger = logging.getLogger(__name__)

MAX_ORDER_PER_SEC = 3
SEMA = asyncio.Semaphore(3)  # 発注 3 req/s 共有


async def _place_order_async(ex, *args, **kwargs):
    """Run synchronous `ex.order` in a thread and return its ACK."""
    res = await asyncio.to_thread(ex.order, *args, **kwargs)
    logging.getLogger(__name__).debug("ORDER-ACK %s", res)
    try:
        st = (res or {}).get("response", {}).get("data", {}).get("statuses", [{}])[0]
        if "error" in st:
            logging.getLogger(__name__).warning("ORDER-ERR %s", st["error"])
    except Exception:
        pass
    return res


def load_pair_yaml(path: str | None) -> dict[str, dict]:
    if not path:
        return {}
    import yaml

    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("bot", help="bot folder name (e.g., pfpl)")
    p.add_argument("--symbols", default="ETH-PERP", help="comma separated list (max 3)")
    p.add_argument("--pair_cfg", help="YAML to override per‑pair params")
    # ─ 共通オプション ─
    p.add_argument("--testnet", action="store_true")
    p.add_argument("--cooldown", type=float, default=1.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log_level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    args = p.parse_args()

    logging.basicConfig(level=args.log_level)

    pair_params = load_pair_yaml(args.pair_cfg)
    symbols = [s.strip() for s in args.symbols.split(",")][:3]

    # 動的 import
    strat_mod = import_module(f"bots.{args.bot}.strategy")
    Strategy = getattr(strat_mod, "PFPLStrategy")

    # WS 生成（1 本）
    ws = WSClient(
        (
            "wss://api.hyperliquid-testnet.xyz/ws"
            if args.testnet
            else "wss://api.hyperliquid.xyz/ws"
        ),
        reconnect=True,
    )

    strategies = []
    for sym in symbols:
        cfg = {
            "target_symbol": sym,
            "testnet": args.testnet,
            "cooldown_sec": args.cooldown,
            "dry_run": args.dry_run,
            **pair_params.get(sym, {}),
        }
        st = Strategy(config=cfg, semaphore=SEMA)  # ★ semaphore を渡す
        strategies.append(st)

    # WS → 全 Strategy へ配信
    async def fanout(msg: dict):
        for st in strategies:
            st.on_message(msg)

    ws.on_message = fanout
    asyncio.create_task(ws.connect())
    await ws.wait_ready()
    for feed in {"allMids", "indexPrices", "oraclePrices"}:  # 乖離検出 feed
        await ws.subscribe(feed)
    await ws.subscribe("fundingInfo")
    await asyncio.Event().wait()  # 常駐


if __name__ == "__main__":
    asyncio.run(main())
