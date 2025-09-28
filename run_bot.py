#!/usr/bin/env python
import argparse
import asyncio
import logging
from decimal import Decimal, ROUND_DOWN
from importlib import import_module
from os import getenv
from pathlib import Path

from hl_core.utils.dotenv_compat import load_dotenv

from hl_core.config import (
    load_settings,
    require_live_creds,
)  # function: .env読込とlive発注の必須チェック

from hl_core.api import WSClient
from hl_core.utils.logger import setup_logger
from hyperliquid.info import Info
from hyperliquid.utils import constants


# env
load_dotenv()

# logger
setup_logger(
    "runner",
    discord_webhook=getenv("DISCORD_WEBHOOK"),  # 追記
)
logger = logging.getLogger(__name__)

MAX_ORDER_PER_SEC = 3
SEMA = asyncio.Semaphore(MAX_ORDER_PER_SEC)  # 発注 3 req/s 共有


async def _place_order_async(ex, *args, **kwargs):
    """Run synchronous `ex.order` in a thread and return its ACK."""
    loop = asyncio.get_running_loop()

    coin = kwargs.get("coin")
    sz = kwargs.get("sz")
    limit_px = kwargs.get("limit_px")
    if coin and (sz is not None or limit_px is not None):
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        meta = await loop.run_in_executor(None, info.meta)
        unit = next((u for u in meta["universe"] if u.get("name") == coin), None)
        if unit:
            # 価格ティック丸め（px_tickの整数倍に切り捨て）
            if limit_px is not None:
                try:
                    px_tick = float(unit.get("pxTick", 0.5))
                except Exception:
                    px_tick = 0.5
                limit_px = float(
                    (Decimal(str(limit_px)) / Decimal(str(px_tick))).to_integral_value(
                        rounding=ROUND_DOWN
                    )
                    * Decimal(str(px_tick))
                )
                kwargs["limit_px"] = limit_px
            # 数量ティック丸め（qty_tickの整数倍に切り捨て＋最小刻み保証）
            if sz is not None:
                try:
                    qty_tick = 10 ** (-unit["szDecimals"])
                except Exception:
                    qty_tick = 0.001
                sz = float(
                    (Decimal(str(sz)) / Decimal(str(qty_tick))).to_integral_value(
                        rounding=ROUND_DOWN
                    )
                    * Decimal(str(qty_tick))
                )
                if sz < qty_tick:
                    sz = qty_tick
                kwargs["sz"] = sz

    res = await asyncio.to_thread(ex.order, *args, **kwargs)
    logger.debug("ORDER-ACK %s", res)
    try:
        st = (res or {}).get("response", {}).get("data", {}).get("statuses", [])
        st = st[0] if st else {}
        if "error" in st:
            logger.warning("ORDER-ERR %s", st.get("error"))

        status_val = (st.get("status") or "").lower()
        if status_val == "filled" or "filled" in st:
            sz = st.get("sz") or st.get("size") or st.get("totalSz")
            px = st.get("px") or st.get("price") or st.get("avgPx")
            oid = st.get("oid") or st.get("orderId")
            logger.info("ORDER-FILLED sz=%s px=%s oid=%s", sz, px, oid)
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
    p.add_argument("--pair_cfg", help="YAML to override per-pair params")
    # ─ 共通オプション ─
    p.add_argument("--testnet", action="store_true")
    p.add_argument("--cooldown", type=float, default=1.0)
    p.add_argument("--order_usd", type=float, default=15.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log_level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    args = p.parse_args()

    # function: 実稼働の安全ゲート（GO/DRY_RUN/NETWORK/鍵）
    settings = load_settings()  # function: .env を読み込み（DRY_RUN/NETWORK/鍵）
    is_mainnet = not args.testnet  # function: --testnet が無指定なら本番(mainnet)
    go_ok = (getenv("GO") or "").strip().lower() in {
        "1",
        "true",
        "live",
        "go",
    }  # function: 明示GOフラグ
    effective_dry_run = (
        args.dry_run or settings.dry_run
    )  # function: CLI/環境のどちらかがtrueならDRY

    if is_mainnet:
        if not go_ok:
            print("[exit] GO が未設定（GO=1|true|live|go）。本番発注を中止。")
            return
        if effective_dry_run:
            print("[exit] DRY_RUN=true（--dry-run または .env）。本番発注を中止。")
            return
        require_live_creds(
            settings
        )  # function: HL_PRIVATE_KEY / HL_ACCOUNT_ADDRESS を必須化

    logging.basicConfig(level=args.log_level)

    pair_params = load_pair_yaml(args.pair_cfg)
    symbols = [s.strip() for s in args.symbols.split(",")][:3]
    bases: set[str] = set()
    for sym in symbols:
        if not sym:
            continue
        base = sym.split("-")[0].strip()
        if base:
            bases.add(base)

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
            "order_usd": args.order_usd,
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

    await ws.subscribe({"type": "allMids"})
    for base in sorted(bases):
        await ws.subscribe({"type": "indexPrices", "coins": [base]})
        await ws.subscribe({"type": "oraclePrices", "coins": [base]})
        await ws.subscribe({"type": "fundingInfo", "coins": [base]})
    await asyncio.Event().wait()  # 常駐


if __name__ == "__main__":
    asyncio.run(main())
