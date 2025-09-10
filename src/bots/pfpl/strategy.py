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
from datetime import datetime  # ← 追加

# 既存 import 群の最後あたりに追加
from hyperliquid.exchange import Exchange
from eth_account.account import Account

setup_logger(bot_name="pfpl")  # ← Bot 切替時はここだけ変える

logger = logging.getLogger(__name__)


class PFPLStrategy:
    """Price-Fair-Price-Lag bot"""

    # ← シグネチャはそのまま
    def __init__(
        self, *, config: dict[str, Any], semaphore: asyncio.Semaphore | None = None
    ):
        # ── ① YAML と CLI のマージ ───────────────────────
        yml_path = Path(__file__).with_name("config.yaml")
        yaml_conf: dict[str, Any] = {}
        if yml_path.exists():
            with yml_path.open(encoding="utf-8") as f:
                yaml_conf = yaml.safe_load(f) or {}
        self.config = {**yaml_conf, **config}
        # --- Funding 直前クローズ用バッファ秒数（デフォルト 120）
        self.funding_close_buffer_secs: int = int(
            getattr(self, "cfg", getattr(self, "config", {})).get(
                "funding_close_buffer_secs", 120
            )
        )
        # --- Order price offset percentage（デフォルト 0.0005 = 0.05 %）
        self.eps_pct: float = float(self.config.get("eps_pct", 0.0005))

        # ── ② 通貨ペア・Semaphore 初期化 ─────────────────
        self.symbol: str = self.config.get("target_symbol", "ETH-PERP")

        max_ops = int(self.config.get("max_order_per_sec", 3))  # 1 秒あたり発注上限
        self.sem: asyncio.Semaphore = semaphore or asyncio.Semaphore(max_ops)

        # 以降 (env 読み込み・SDK 初期化 …) は従来コードを続ける
        # ------------------------------------------------------------------

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
        self.mid: Decimal | None = None  # 板 Mid (@1)
        self.idx: Decimal | None = None  # indexPrices
        self.ora: Decimal | None = None  # oraclePrices

        # ── 内部ステート ────────────────────────────────
        self.last_side: str | None = None
        self.last_ts: float = 0.0
        self.pos_usd = Decimal("0")
        # ★ Funding Guard 用
        self.next_funding_ts: float | None = None  # 次回資金調達の UNIX 秒
        self._funding_pause: bool = False  # True なら売買停止
        self.next_funding_ts: int | None = None  # 直近 funding 予定 UNIX 秒
        self._funding_pause: bool = False  # True: 売買停止中
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
        ch = msg.get("channel")

        if ch == "allMids":  # 板 mid 群
            self.mid = Decimal(msg["data"]["mids"]["@1"])
        elif ch == "indexPrices":  # インデックス価格
            self.idx = Decimal(msg["data"]["prices"]["ETH"])
        elif ch == "oraclePrices":  # オラクル価格
            self.ora = Decimal(msg["data"]["prices"]["ETH"])
        elif ch == "fundingInfo":
            data = msg.get("data", {})
            next_ts = data.get("nextFundingTime")
            if next_ts is None:
                info = data.get(self.symbol)
                if info:
                    next_ts = info.get("nextFundingTime")
            if next_ts is not None:
                self.next_funding_ts = int(next_ts)
                logger.debug("fundingInfo: next @ %s", self.next_funding_ts)

        # fair が作れれば評価へ
        if self.mid and self.idx and self.ora:
            self.fair = (self.idx + self.ora) / 2  # ★ 平均で公正価格
            self.evaluate()

    # ---------------------------------------------------------------- evaluate

    # src/bots/pfpl/strategy.py
    # ------------------------------------------------------------------ Tick loop
    # ─────────────────────────────────────────────────────────────
    def evaluate(self) -> None:
        if not self._check_funding_window():
            return
        # ── fair / mid がまだ揃っていないなら何もしない ─────────
        if self.mid is None or self.fair is None:
            return
        now = time.time()
        # 0) --- Funding 直前クローズ判定 -----------------------------------
        # 0) --- Funding 直前クローズ判定 -----------------------------------
        if self._should_close_before_funding(now):
            asyncio.create_task(self._close_all_positions())
            return  # 今回の evaluate はここで終了

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

    async def place_order(
        self,
        side: str,
        size: float,
        *,
        order_type: str = "limit",
        limit_px: float | None = None,
        **kwargs,
    ) -> None:
        """IOC で即時約定、失敗時リトライ付き"""
        is_buy = side == "BUY"

        # ── Dry-run ───────────────────
        if self.config.get("dry_run"):
            logger.info("[DRY-RUN] %s %.4f %s", side, size, self.symbol)
            self.last_ts = time.time()
            self.last_side = side
            return
        # ──────────────────────────────

        # --- eps_pct を適用した価格補正 -------------------------------
        if order_type == "limit":
            limit_px = (
                limit_px
                if limit_px is not None
                else self._price_with_offset(float(self.mid), side)
            )

        async with self.sem:  # 1 秒あたり発注制御
            MAX_RETRY = 3
            for attempt in range(1, MAX_RETRY + 1):
                try:
                    resp = self.exchange.order(
                        coin=self.symbol,
                        is_buy=is_buy,
                        sz=float(size),
                        limit_px=limit_px,
                        order_type={"limit": {"tif": "Ioc"}},  # IOC 指定
                        reduce_only=False,
                    )
                    logger.info("ORDER OK %s try=%d → %s", self.symbol, attempt, resp)
                    self._order_count += 1
                    self.last_ts = time.time()
                    self.last_side = side
                    break
                except Exception as exc:
                    logger.error(
                        "ORDER FAIL %s try=%d/%d: %s",
                        self.symbol,
                        attempt,
                        MAX_RETRY,
                        exc,
                    )
                    if attempt == MAX_RETRY:
                        logger.error(
                            "GIVE-UP %s after %d retries", self.symbol, MAX_RETRY
                        )
                    else:
                        await anyio.sleep(0.5)

    def _sign(self, payload: dict[str, Any]) -> str:
        """API Wallet Secret で HMAC-SHA256 署名（例）"""
        msg = json.dumps(payload, separators=(",", ":")).encode()
        return hmac.new(self.secret.encode(), msg, hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------ limits
    def _check_limits(self) -> bool:
        """日次の発注数と DD 制限を超えていないか確認"""
        today = datetime.utcnow().date()
        if today != self._start_day:  # 日付が変わったらリセット
            self._start_day = today
            self._order_count = 0

        if self._order_count >= self.max_order_per_sec * 60 * 60 * 24:
            logger.warning("daily order-limit reached → trading disabled")
            return False

        if abs(self.pos_usd) >= self.max_pos:
            logger.warning("position limit %.2f USD reached", self.max_pos)
            return False

        if self.drawdown_usd >= self.max_drawdown_usd:
            logger.warning("drawdown limit %.2f USD reached", self.max_drawdown_usd)
            return False

        return True

    def _check_funding_window(self) -> bool:
        """
        funding 直前・直後は True を返さず evaluate() を停止させる。
        - 5 分前 〜 2 分後 を「危険窓」とする
        """
        if self.next_funding_ts is None:
            return True  # fundingInfo 未取得なら通常運転

        now = time.time()
        before = 300  # 5 分前
        after = 120  # 2 分後

        in_window = self.next_funding_ts - before <= now <= self.next_funding_ts + after

        if in_window and not self._funding_pause:
            logger.info("⏳ Funding window ➜ 売買停止")
            self._funding_pause = True
        elif not in_window and self._funding_pause:
            logger.info("✅ Funding passed ➜ 売買再開")
            self._funding_pause = False

        return not in_window

    # ------------------------------------------------------------------
    # Funding‑close helper
    # ------------------------------------------------------------------
    def _should_close_before_funding(self, now_ts: float) -> bool:
        """Return True if we are within the configured buffer before funding."""
        next_ts = getattr(self, "next_funding_ts", None)
        if not next_ts:
            return False
        return now_ts > next_ts - self.funding_close_buffer_secs

    async def _close_all_positions(self) -> None:
        """Close every open position for this symbol."""
        # --- 成行 IOC で反対サイドを投げる簡易版 ---
        pos = await self.client.get_position(self.symbol)  # ← API に合わせて修正
        if not pos or pos["size"] == 0:
            return  # 持ち高なし
        close_side = "SELL" if pos["size"] > 0 else "BUY"
        await self.place_order(
            side=close_side,
            size=abs(pos["size"]),
            order_type="market",
            reduce_only=True,
            comment="auto‑close‑before‑funding",
        )
        self.logger.info(
            "⚡ Funding close: %s %s @ %s (buffer %s s)",
            close_side,
            abs(pos["size"]),
            self.symbol,
            self.funding_close_buffer_secs,
        )

    # ------------------------------------------------------------------
    # Order-price helper
    # ------------------------------------------------------------------
    def _price_with_offset(self, base_px: float, side: str) -> float:
        """
        Shift `base_px` by eps_pct toward the favourable direction.

        BUY  → base_px * (1 - eps_pct)   (より安く買う)
        SELL → base_px * (1 + eps_pct)   (より高く売る)
        """
        if side.upper() == "BUY":
            return base_px * (1 - self.eps_pct)
        return base_px * (1 + self.eps_pct)
