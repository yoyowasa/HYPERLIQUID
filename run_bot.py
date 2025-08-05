#!/usr/bin/env python
import argparse
from typing import Any
import asyncio
from importlib import import_module
from os import getenv
from pathlib import Path
import json
from dotenv import load_dotenv
import sys
import yaml
from hl_core.api import WSClient
from hl_core.utils.logger import setup_logger
from hl_core.api import HTTPClient
from bots.pfpl import run_live as run_pfpl
import logging

logging.getLogger("websockets").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.INFO)

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

    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


async def main() -> None:
    p = argparse.ArgumentParser()
    # ----------------- ここまで追加 -----------------
    p.add_argument("bot", help="bot folder name (e.g., pfpl)")
    p.add_argument(
        "--symbols", default=None, help="comma list (default: all pairs in YAML)"
    )
    p.add_argument(
        "--pair_cfg", default="src/bots/pfpl/pairs.yaml", help="pair YAML path"
    )
    # ─ 共通オプション ─
    p.add_argument("--testnet", action="store_true")
    p.add_argument(
        "--order_usd",
        type=float,
        default=None,
        help="Order notional per trade (USD)",
    )

    p.add_argument(
        "--min_usd",
        type=float,
        help="Override minimum order notional (USD) set in YAML",
    )
    p.add_argument("--max_pos", type=float, help="Override position limit (USD)")

    p.add_argument("--cooldown", type=float, default=1.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log_level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    args = p.parse_args()

    # --- ログ設定 -------------------------------------------------

    root = logging.getLogger()
    root.setLevel(args.log_level)  # ① root を指定レベルに

    console = logging.StreamHandler(sys.stdout)  # ② コンソールハンドラ
    console.setLevel(args.log_level)
    console.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root.addHandler(console)  # ③ root に付ける

    # bots.pfpl 系も同じレベルを強制（念のため）

    logging.getLogger("bots.pfpl").setLevel(args.log_level)

    pair_params = load_pair_yaml(args.pair_cfg)
    logger.info("PAIR-DEBUG loaded=%s", pair_params.get("BTC-PERP"))

    symbols = [
        s.strip()
        for s in (args.symbols or ",".join(load_pair_yaml(args.pair_cfg).keys())).split(
            ","
        )
    ][:3]

    # 動的 import
    strat_mod = import_module(f"bots.{args.bot}.strategy")
    Strategy = getattr(strat_mod, "PFPLStrategy")
    # --- SDK(HTTP) クライアントを生成 ---------------------------------
    base_url = (
        "https://api.hyperliquid-testnet.xyz"
        if args.testnet
        else "https://api.hyperliquid.xyz"
    )
    sdk = HTTPClient(base_url, api_key=getenv("HL_API_SECRET"))

    # WS 生成（1 本）
    ws = WSClient(
        (
            "wss://api.hyperliquid-testnet.xyz/ws"
            if args.testnet
            else "wss://api.hyperliquid.xyz/ws"
        ),
        reconnect=True,
    )
    api_client = sdk  # ← REST 用
    ws_client = ws  # ← WebSocket 用
    tx_client = sdk  # ← 発注も REST を暫定利用

    asyncio.create_task(run_pfpl(api_client, ws_client, tx_client))

    strategies = []
    for sym in symbols:
        # --- ① YAML 基準でベース設定を作る -------------------------------
        base_cfg: dict[str, Any] = {
            **pair_params.get(sym, {}),  # ペア別 YAML（最優先）
        }
        base_cfg["target_symbol"] = sym

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
        if args.max_pos is not None:
            base_cfg["max_pos"] = args.max_pos

        # --- ③ Strategy インスタンス生成 ---------------------------------
        st = Strategy(config=base_cfg, semaphore=SEMA, sdk=sdk)  # ★ semaphore を渡す
        strategies.append(st)

    # WS → 全 Strategy へ配信
    async def fanout(msg: dict):
        logger.debug("WS-RAW: %s", msg)
        for st in strategies:
            st.on_message(msg)

    ws.on_message = fanout
    # ── WebSocket 接続 --------------------------------------------------
    asyncio.create_task(ws.connect())
    await ws.wait_ready()  # ← 接続が確立してから購読

    # ── 必須フィードを subscribe ----------------------------------------
    await ws.subscribe("allMids")  # 任意: 全銘柄板ミッド

    # run_bot.py  購読ループ
    for sym in symbols:
        coin_base = sym.split("-")[0]  # "ETH"

        # 公正価格 + Funding
        await ws._ws.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "subscription": {"type": "activeAssetCtx", "coin": coin_base},
                }
            )
        )

        # best-bid/ask → mid
        await ws._ws.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "subscription": {"type": "bbo", "coin": coin_base},
                }
            )
        )

        # blocks → 高さ & タイムスタンプ
        await ws._ws.send(
            json.dumps({"method": "subscribe", "subscription": {"type": "blocks"}})
        )

        # openInterest → OI Long / Short
        await ws._ws.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "subscription": {"type": "openInterest", "coin": coin_base},
                }
            )
        )

        # Funding
        funding_feed = "fundingInfoTestnet" if args.testnet else "fundingInfo"
        await ws._ws.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "subscription": {"type": funding_feed, "coin": coin_base},
                }
            )
        )

    # （ループ後の重複 bbo ブロックは削除）

    logger.debug(
        "SEND-DEBUG: %s",
        json.dumps(
            {
                "method": "subscribe",
                "subscription": {"type": "bbo", "coin": "coin_base"},
            }
        ),
    )

    # -------------------------------------------------------------------

    # -----------------------------------------------------------------

    # # ③ Funding
    # funding_feed = "fundingInfoTestnet" if args.testnet else "fundingInfo"
    # for sym in symbols:
    #     coin = sym.split("-")[0]  # "ETH" or "BTC"
    #     await ws._ws.send(
    #         json.dumps(
    #             {
    #                 "method": "subscribe",
    #                 "subscription": {"type": funding_feed, "coin": coin},
    #             }
    #         )
    #     )
    # ------------------------------------------------------------------

    await asyncio.Event().wait()  # 常駐


if __name__ == "__main__":
    asyncio.run(main())
