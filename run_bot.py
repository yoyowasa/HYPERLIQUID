#!/usr/bin/env python
import argparse
from typing import Any
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
    p.add_argument(
        "--order_usd",
        type=float,
        default=10.0,
        help="Order notional per trade (USD)",
    )
    p.add_argument(
        "--min_usd",
        type=float,
        help="Override minimum order notional (USD) set in YAML",
    )

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
        # --- ① YAML 基準でベース設定を作る -------------------------------
        base_cfg: dict[str, Any] = {
            **pair_params.get(sym, {}),  # ペア別 YAML（最優先）
        }

        # --- ② CLI から渡された値だけピンポイント上書き -----------------
        if args.order_usd is not None:
            base_cfg["order_usd"] = args.order_usd
        if args.min_usd is not None:
            base_cfg["min_usd"] = args.min_usd
        if args.cooldown is not None:
            base_cfg["cooldown_sec"] = args.cooldown
        if args.dry_run:
            base_cfg["dry_run"] = True
        if args.testnet:
            base_cfg["testnet"] = True
        if args.log_level:
            base_cfg["log_level"] = args.log_level

        # --- ③ Strategy インスタンス生成 ---------------------------------
        st = Strategy(config=base_cfg, semaphore=SEMA)  # ★ semaphore を渡す
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
