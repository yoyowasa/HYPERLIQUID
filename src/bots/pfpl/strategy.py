# src/bots/pfpl/strategy.py
from __future__ import annotations
import os
import logging
from typing import Any
import asyncio
import hmac
import hashlib
import json
from time import time
from decimal import Decimal
from hl_core.utils.logger import setup_logger

# 既存 import 群の最後あたりに追加
from hyperliquid.exchange import Exchange
from eth_account.account import Account

setup_logger(bot_name="pfpl")  # ← Bot 切替時はここだけ変える

logger = logging.getLogger(__name__)


class PFPLStrategy:
    """Price‑Fair‑Price‑Lag bot"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.mids: dict[str, str] = {}

        # env keys
        self.account = os.getenv("HL_ACCOUNT_ADDR")
        self.secret = os.getenv("HL_API_SECRET")
        if not (self.account and self.secret):
            raise RuntimeError("HL_ACCOUNT_ADDR / HL_API_SECRET が未設定")

        # Hyperliquid SDK
        # Hyperliquid SDK
        self.wallet = Account.from_key(self.secret)
        base_url = (
            "https://api.hyperliquid-testnet.xyz"  # テストネット
            if config.get("testnet")
            else "https://api.hyperliquid.xyz"  # メインネット
        )
        self.exchange = Exchange(
            self.wallet,  # ① wallet (LocalAccount)
            base_url,  # ② base_url 文字列
            account_address=self.account,
        )

        # meta info
        meta = self.exchange.info.meta()
        # テストネットには minSizeUsd が無い場合がある → フォールバック
        # minSizeUsd が Testnet には無い場合がある → フォールバック
        min_usd_map: dict[str, str] = meta.get("minSizeUsd", {})
        if not min_usd_map:
            logger.warning("minSizeUsd not present in meta; defaulting to USD 10")
            min_usd_map = {"ETH": "10"}  # ← 必要なら YAML で上書き可
        self.min_usd = Decimal(min_usd_map["ETH"])
        uni_eth = next(asset for asset in meta["universe"] if asset["name"] == "ETH")
        tick_raw = uni_eth.get("pxTick", uni_eth.get("pxTickSize", "0.01"))
        self.tick = Decimal(tick_raw)

        # params
        self.cooldown = float(config.get("cooldown_sec", 1.0))
        self.order_usd = Decimal(config.get("order_usd", 10))
        self.max_pos = Decimal(config.get("max_position_usd", 100))

        # state ---------------------------------------------------------------
        self.last_side: str | None = None  # 直前に出したサイド
        self.last_ts: float = 0.0  # 直前発注の UNIX 秒
        self.pos_usd: Decimal = Decimal("0")  # 現在ポジション USD

        logger.info("PFPLStrategy initialised with %s", config)

    # ------------------------------------------------------------------ WS hook

    def on_message(self, msg: dict[str, Any]) -> None:
        if msg.get("channel") != "allMids":
            return
        self.mids = msg["data"]["mids"]
        self.evaluate()
        # --- 受信データから現在ポジション USD を更新 ---
        acct = self.exchange.info.account(self.account)
        self.pos_usd = Decimal(acct["marginSummary"]["totalValue"])  # 現物で十分

    # ---------------------------------------------------------------- evaluate

    def evaluate(self) -> None:
        now = time.time()
        # --- クールダウン ---
        if now - self.last_ts < self.cooldown:
            return  # まだクールダウン中

        # --- ポジション上限 ---
        if abs(self.pos_usd) >= self.max_pos:
            return  # 上限到達

        mid = Decimal(self.mids.get("@1", "0"))
        fair = Decimal(self.mids.get("@10", "0"))  # ダミー: 本来は別 feed
        spread = fair - mid
        threshold = Decimal("0.01")

        if abs(spread) < threshold:
            return

        side = "BUY" if spread < 0 else "SELL"
        # --- 直前と同じサイドなら発注を抑制 -----------------------------
        if side == self.last_side:
            logger.debug("same side as previous (%s) → skip", side)
            return

        # duplicate suppress
        now = time.time()
        if side == self.last_side and now - self.last_ts < self.cooldown:
            return
        self.last_side, self.last_ts = side, now

        # USD→サイズ計算 & フィルタ
        size = (self.order_usd / mid).quantize(self.tick)
        if size * mid < self.min_usd:
            logger.debug("skip: %s USD < minSizeUsd", size * mid)
            return

        # ポジション超過チェック
        pos = Decimal(self.exchange.position()["size"]) * mid
        if pos + size * mid > self.max_pos:
            logger.warning("skip: pos %.2f > max %.2f", pos, self.max_pos)
            return

        asyncio.create_task(self.place_order(side, float(size)))

    # ---------------------------------------------------------------- order

    async def place_order(self, side: str, size: float) -> None:
        is_buy = side == "BUY"
        MAX_RETRY = 3
        for attempt in range(1, MAX_RETRY + 1):  # ← ここから
            try:
                resp = self.exchange.order(
                    name="ETH",
                    is_buy=is_buy,
                    sz=size,
                    limit_px=1e9 if is_buy else 1e-9,
                    order_type={"limit": {"tif": "Ioc"}},
                    reduce_only=False,
                )
                if resp.get("status") != "ok":  # SDK 正常判定
                    raise RuntimeError(f"order failed: {resp}")
                # --- 成功処理 ---
                self.last_ts = time.time()
                self.last_side = side
                logger.info("ORDER RESP: %s", resp)
                return  # 成功で抜ける
            except Exception as exc:
                logger.error("ORDER ERR (%d/%d): %s", attempt, MAX_RETRY, exc)
                if attempt == MAX_RETRY:
                    return  # 最後でも失敗なら諦める
                await asyncio.sleep(0.5 * attempt)

    def _sign(self, payload: dict[str, Any]) -> str:
        """API Wallet Secret で HMAC-SHA256 署名（例）"""
        msg = json.dumps(payload, separators=(",", ":")).encode()
        return hmac.new(self.secret.encode(), msg, hashlib.sha256).hexdigest()
