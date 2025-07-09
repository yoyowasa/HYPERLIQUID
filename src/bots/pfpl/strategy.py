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

    def __init__(self, *, config: dict[str, Any], semaphore: asyncio.Semaphore | None = None):
        self.sem = semaphore or asyncio.Semaphore(config.get("max_order_per_sec", 3))


        self.sem = semaphore  # 発注レート共有
        self.symbol = config.get("target_symbol", "ETH-PERP")
        # ── YAML + CLI マージ ─────────────────────────────
        yml_path = Path(__file__).with_name("config.yaml")
        yaml_conf: dict[str, Any] = {}
        if yml_path.exists():
            with yml_path.open(encoding="utf-8") as f:
                yaml_conf = yaml.safe_load(f) or {}
        self.config = {**yaml_conf, **config}
        self.mids: dict[str, str] = {}

        # ── 環境変数キー ────────────────────────────────
        self.account = os.getenv("HL_ACCOUNT_ADDR")
        self.secret = os.getenv("HL_API_SECRET")
        if not (self.account and self.secret):
            raise RuntimeError("HL_ACCOUNT_ADDR / HL_API_SECRET が未設定")

        # ── Hyperliquid SDK 初期化 ──────────────────────
        self.wallet = Account.from_key(self.secret)
        base_url = (
            "https://api.hyperliquid-testnet.xyz"
            if self.config.get("testnet")
            else "https://api.hyperliquid.xyz"
        )
        self.exchange = Exchange(
            self.wallet,
            base_url,
            account_address=self.account,
        )

        # ── meta 情報から tick / min_usd 決定 ───────────
        meta = self.exchange.info.meta()

        # min_usd
        if min_usd_cfg := self.config.get("min_usd"):
            self.min_usd = Decimal(str(min_usd_cfg))
            logger.info("min_usd override from config: USD %.2f", self.min_usd)
        else:
            min_usd_map: dict[str, str] = meta.get("minSizeUsd", {})
            self.min_usd = (
                Decimal(min_usd_map["ETH"]) if "ETH" in min_usd_map else Decimal("1")
            )
            if "ETH" not in min_usd_map:
                logger.warning("minSizeUsd missing ➜ fallback USD 1")

        # tick
        uni_eth = next(u for u in meta["universe"] if u["name"] == "ETH")
        tick_raw = uni_eth.get("pxTick") or uni_eth.get("pxTickSize", "0.01")
        self.tick = Decimal(tick_raw)

        # ── Bot パラメータ ──────────────────────────────
        self.cooldown = float(self.config.get("cooldown_sec", 1.0))
        self.order_usd = Decimal(self.config.get("order_usd", 10))
        self.max_pos = Decimal(self.config.get("max_position_usd", 100))
        self.fair_feed = self.config.get("fair_feed", "indexPrices")
        self.max_daily_orders = int(self.config.get("max_daily_orders", 500))
        self.max_drawdown_usd = Decimal(self.config.get("max_drawdown_usd", 100))
        self._order_count = 0
        self._start_day = datetime.utcnow().date()
        self.enabled = True
        # ── フィード保持用 -------------------------------------------------
        self.mid:  Decimal | None = None   # 板 Mid (@1)
        self.idx:  Decimal | None = None   # indexPrices
        self.ora:  Decimal | None = None   # oraclePrices

        # ── 内部ステート ────────────────────────────────
        self.last_side: str | None = None
        self.last_ts: float = 0.0
        self.pos_usd = Decimal("0")

        # 非同期でポジション初期化
        try:
            asyncio.get_running_loop().create_task(self._refresh_position())
        except RuntimeError:
            pass  # pytest 収集時など、イベントループが無い場合

        # ─── ここから追加（ロガーをペアごとのファイルへも出力）────
        h = logging.FileHandler(f"strategy_{self.symbol}.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(h)

        logger.info("PFPLStrategy initialised with %s", self.config)

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
    # ------------------------------------------------------------------ WS hook
    def on_message(self, msg: dict[str, Any]) -> None:
        if msg.get("channel") == "allMids":          # 板 mid 群
            self.mid = Decimal(msg["data"]["mids"]["@1"])
        elif msg.get("channel") == "indexPrices":    # インデックス価格
            self.idx = Decimal(msg["data"]["prices"]["ETH"])
        elif msg.get("channel") == "oraclePrices":   # オラクル価格
            self.ora = Decimal(msg["data"]["prices"]["ETH"])

        # fair が作れれば評価へ
        if self.mid and self.idx and self.ora:
            self.fair = (self.idx + self.ora) / 2      # ★ 平均で公正価格
            self.evaluate()


    # ---------------------------------------------------------------- evaluate

    # src/bots/pfpl/strategy.py
    # ------------------------------------------------------------------ Tick loop
    # ─────────────────────────────────────────────────────────────
    def evaluate(self) -> None:
        # ── fair / mid がまだ揃っていないなら何もしない ─────────
        if self.mid is None or self.fair is None:
            return
        now = time.time()
        # --- リスクガード ------------------
        if not self._check_limits():
            return
        # ① クールダウン判定
        if now - self.last_ts < self.cooldown:
            return

        # ② 最大建玉判定
        if abs(self.pos_usd) >= self.max_pos:
            return

        # ③ 必要データ取得
        mid = Decimal(self.mids.get("@1", "0"))
        fair = Decimal(self.mids.get(self.fair_feed, "0"))
        if mid == 0 or fair == 0:
            return  # データが揃っていない

        abs_diff = abs(fair - mid)  # USD 差
        pct_diff = abs_diff / mid * Decimal("100")  # 乖離率 %

        # ④ 閾値判定
        th_abs = Decimal(str(self.config.get("threshold", "1.0")))  # USD
        th_pct = Decimal(str(self.config.get("threshold_pct", "0.05")))  # %
        mode = self.config.get("mode", "both")  # both / either

        if mode == "abs":
            if abs_diff < th_abs:
                return
        elif mode == "pct":
            if pct_diff < th_pct:
                return
        elif mode == "either":
            if abs_diff < th_abs and pct_diff < th_pct:
                return
        else:  # default = both
            if abs_diff < th_abs or pct_diff < th_pct:
                return

        # ⑤ 発注サイド決定
        side = "BUY" if fair < mid else "SELL"

        # ⑥ 連続同方向防止
        if side == self.last_side and now - self.last_ts < self.cooldown:
            return

        # ⑦ 発注サイズ計算
        size = (self.order_usd / mid).quantize(self.tick)
        if size * mid < self.min_usd:
            logger.debug(
                "size %.4f USD %.2f < min_usd %.2f → skip",
                size,
                size * mid,
                self.min_usd,
            )
            return

        # ⑧ 建玉超過チェック
        if (
            abs(self.pos_usd + (size * mid if side == "BUY" else -size * mid))
            > self.max_pos
        ):
            logger.debug("pos_limit %.2f USD 超過 → skip", self.max_pos)
            return

        # ⑨ 発注
        asyncio.create_task(self.place_order(side, float(size)))

    # ---------------------------------------------------------------- order

    async def place_order(self, side: str, size: float) -> None:
        async with self.sem:  # ★ 3 req/s 保証
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
    async with self.sem:
        MAX_RETRY = 3
        for attempt in range(1, MAX_RETRY + 1):
            try:
                resp = self.exchange.order(
                    coin=self.symbol,
                    is_buy=is_buy,
                    sz=size,
                    limit_px=limit_px,
                    order_type={"limit": {"tif": "Ioc"}},  # IOC 指定
                    reduce_only=False,
                )
                logger.info("ORDER OK (try %d): %s", attempt, resp)
                self._order_count += 1
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
    # ------------------------------------------------------------------ limits
    def _check_limits(self) -> bool:
        """
        取引可否を判定するガード:
        - 日次発注回数
        - ドローダウン USD
        True を返せば発注を継続、False で停止
        """
        # 日付が変わったらリセット
        today = datetime.utcnow().date()
        if today != self._start_day:
            self._order_count = 0
            self._start_day = today

        # PnL / ドローダウンを取得（SDK で accountValue などが取れる想定）
        try:
            state = self.exchange.info.user_state(self.account)
            pnl = Decimal(state["marginSummary"]["totalPnlUsd"])
        except Exception:
            pnl = Decimal("0")  # 失敗したら無視して続行

        if pnl < -self.max_drawdown_usd:
            logger.error("DD %.2f USD > limit %.2f ➜ bot disabled", pnl, self.max_drawdown_usd)
            self.enabled = False

        if self._order_count >= self.max_daily_orders:
            logger.warning("daily order limit %d hit ➜ bot disabled", self.max_daily_orders)
            self.enabled = False

        return self.enabled
