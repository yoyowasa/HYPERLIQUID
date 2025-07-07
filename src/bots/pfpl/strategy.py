# src/bots/pfpl/strategy.py
from __future__ import annotations
import os
import logging
from typing import Any
import asyncio
import hmac
import hashlib
import json
import time
from decimal import Decimal
from hl_core.utils.logger import setup_logger
from pathlib import Path
import yaml
import anyio

# 既存 import 群の最後あたりに追加
from hyperliquid.exchange import Exchange
from eth_account.account import Account

setup_logger(bot_name="pfpl")  # ← Bot 切替時はここだけ変える

logger = logging.getLogger(__name__)


class PFPLStrategy:
    """Price‑Fair‑Price‑Lag bot"""

    def __init__(self, config: dict[str, Any]) -> None:
        # --- YAML 取り込み ------------------------------------------------
        yml_path = Path(__file__).with_name("config.yaml")
        yaml_conf: dict[str, Any] = {}
        if yml_path.exists():
            with yml_path.open(encoding="utf-8") as f:
                yaml_conf = yaml.safe_load(f) or {}
        self.config = {**yaml_conf, **config}
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
        self.cooldown = float(self.config.get("cooldown_sec", 1.0))
        self.order_usd = Decimal(self.config.get("order_usd", 10))
        self.max_pos = Decimal(self.config.get("max_position_usd", 100))

        self.fair_feed = self.config.get("fair_feed", "@10")  # デフォルト @10

        # state ---------------------------------------------------------------
        self.last_side: str | None = None  # 直前に出したサイド
        self.last_ts: float = 0.0  # 直前発注の UNIX 秒
        self.pos_usd: Decimal = Decimal("0")  # 現在ポジション USD
        # 🔽 起動ループがあればバックグラウンドで最新化
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._refresh_position())
        except RuntimeError:
            # まだイベントループが無い（pytest 収集中など）→後で evaluate() から取る
            pass

        logger.info("PFPLStrategy initialised with %s", config)

    # ── src/bots/pfpl/strategy.py ──
    async def _refresh_position(self) -> None:
        """
        現在の ETH-PERP 建玉 USD を self.pos_usd に反映。
        perpPositions が無い口座でも落ちない。
        """
        try:
            state = self.exchange.info.user_state(self.account)

            # ―― ETH の perp 建玉を抽出（無い場合は None）
            perp_pos = next(
                (
                    p
                    for p in state.get("perpPositions", [])  # ← 🔑 get(..., [])
                    if p["position"]["coin"] == "ETH"
                ),
                None,
            )

            usd = (
                Decimal(perp_pos["position"]["sz"])
                * Decimal(perp_pos["position"]["entryPx"])
                if perp_pos
                else Decimal("0")
            )
            self.pos_usd = usd
            logger.debug("pos_usd refreshed: %.2f", usd)
        except Exception as exc:  # ← ここで握りつぶす
            logger.warning("refresh_position failed: %s", exc)

    # ② ────────────────────────────────────────────────────────────
    async def on_message(self, msg: dict[str, Any]) -> None:
        """
        allMids チャネルを受信するたびに
        1) mid 情報を更新
        2) 売買判定 evaluate()
        3) ポジション情報を最新化 (_refresh_position)
        """
        if msg.get("channel") != "allMids":
            return

        self.mids = msg["data"]["mids"]

        # 売買ロジック
        self.evaluate()

        # ポジション更新（await 必要ない設計なら同期呼び出しでも可）
        # ここは asyncio.create_task(...) で fire-and-forget にしておくと
        # on_message をブロックしない。
        await self._refresh_position()

    # ---------------------------------------------------------------- evaluate

    # src/bots/pfpl/strategy.py
    def evaluate(self) -> None:
        now = time.time()

        # ── クールダウン ─────────────────────────
        if now - self.last_ts < self.cooldown:
            return

        # ── 最大ポジション超過チェック ────────────
        if abs(self.pos_usd) >= self.max_pos:
            return

        # ── ミッド／フェア取得 ────────────────────
        mid = Decimal(self.mids.get("@1", "0"))
        fair = Decimal(self.mids.get(self.fair_feed, "0"))
        if mid == 0 or fair == 0:
            return  # データ欠損

        spread_abs = (fair - mid).copy_abs()  # 絶対 USD 差
        spread_pct = (spread_abs / mid) * Decimal("100")  # ％差

        # ── コンフィグしきい値 ────────────────────

        pct_th = Decimal(str(self.config.get("spread_threshold_pct", 0)))

        # ★ 今は “%” だけ判定
        if spread_pct < pct_th:
            return

        side = "BUY" if fair < mid else "SELL"

        # ── 連続同サイド抑制 ──────────────────
        if side == self.last_side:
            logger.debug("same side as previous (%s) → skip", side)
            return

        # ── 発注サイズ計算 ──────────────────────
        size = (self.order_usd / mid).quantize(self.tick)
        if size * mid < self.min_usd:
            logger.debug("size %.4f USD < minSizeUsd, skip", size * mid)
            return

        # ── 現在ポジ更新 & 上限チェック ──────────
        asyncio.create_task(self._refresh_position())
        if self.pos_usd + (size * mid if side == "BUY" else -size * mid) > self.max_pos:
            logger.warning("pos %.2f > max %.2f, skip", self.pos_usd, self.max_pos)
            return

        asyncio.create_task(self.place_order(side, float(size)))

        # クールダウン用のタイムスタンプとサイドを更新
        self.last_side, self.last_ts = side, now

    # ---------------------------------------------------------------- order

    async def place_order(self, side: str, size: float) -> None:
        is_buy = side == "BUY"

        # ---------- ① Dry‑run 判定 ----------
        if self.config.get("dry_run"):
            logger.info("[DRY-RUN] %s %.4f", side, size)
            self.last_ts = time.time()
            self.last_side = side
            return
        # ------------------------------------

        # ---------- ② 指値価格を計算 ----------
        #   ε = price_buffer_pct (％表記)   ← config.yaml で調整可
        eps_pct = float(self.config.get("price_buffer_pct", 2.0))  # 既定 2 %
        mid = float(self.mids.get("@1", "0") or 0)  # failsafe 0
        if mid == 0:
            logger.warning("mid price unknown → skip order")
            return

        factor = 1.0 + eps_pct / 100.0
        limit_px = mid * factor if is_buy else mid / factor
        # -------------------------------------

        MAX_RETRY = 3
        for attempt in range(1, MAX_RETRY + 1):
            try:
                resp = self.exchange.order(
                    coin="ETH",
                    is_buy=is_buy,
                    sz=size,
                    limit_px=limit_px,
                    order_type={"limit": {"tif": "Ioc"}},  # IOC 指定
                    reduce_only=False,
                )
                logger.info("ORDER OK (try %d): %s", attempt, resp)
                self.last_ts = time.time()
                self.last_side = side
                break  # 成功したら抜ける
            except Exception as exc:
                logger.error("ORDER FAIL (try %d/%d): %s", attempt, MAX_RETRY, exc)
                if attempt == MAX_RETRY:
                    logger.error("GIVE UP after %d retries", MAX_RETRY)
                else:
                    await anyio.sleep(0.5)

    def _sign(self, payload: dict[str, Any]) -> str:
        """API Wallet Secret で HMAC-SHA256 署名（例）"""
        msg = json.dumps(payload, separators=(",", ":")).encode()
        return hmac.new(self.secret.encode(), msg, hashlib.sha256).hexdigest()
