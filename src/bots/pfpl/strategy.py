# src/bots/pfpl/strategy.py
from __future__ import annotations
import os
import logging
import sys
from typing import Any
import asyncio
import hmac
import hashlib
import json
import time
from decimal import Decimal, ROUND_UP
from hl_core.utils.logger import setup_logger
from pathlib import Path
import yaml
import anyio
from datetime import datetime  # ← 追加
from hl_core.utils.analysis_logger import AnalysisLogger

# 旧: from hl_core.utils.notify import line_notify
from hl_core.utils.notify import discord_notify

# 既存 import 群の最後あたりに追加
from hyperliquid.exchange import Exchange
from eth_account.account import Account

setup_logger(bot_name="pfpl")  # ← Bot 切替時はここだけ変える

logger = logging.getLogger(__name__)


class PFPLStrategy:
    """Price-Fair-Price-Lag bot"""

    # ← シグネチャはそのまま
    def __init__(
        self, *, config: dict[str, Any], sdk, semaphore: asyncio.Semaphore | None = None
    ):
        # ── ① YAML と CLI のマージ ───────────────────────
        yml_path = Path(__file__).with_name("config.yaml")
        yaml_conf: dict[str, Any] = {}
        if yml_path.exists():
            with yml_path.open(encoding="utf-8") as f:
                yaml_conf = yaml.safe_load(f) or {}
        self.config = {**yaml_conf, **config}
        self.meta: dict[str, Any] = {}
        self.fair: Decimal | None = None  # ← フェア価格の初期値
        self.mids: dict[str, str] = {}
        # --- Funding 直前クローズ用バッファ秒数（デフォルト 120）
        self.funding_close_buffer_secs: int = int(
            getattr(self, "cfg", getattr(self, "config", {})).get(
                "funding_close_buffer_secs", 120
            )
        )
        # --- Order price offset percentage（デフォルト 0.0005 = 0.05 %）
        self.eps_pct: float = float(self.config.get("eps_pct", 0.0005))
        # --- Minimum equity ratio guard（デフォルト 0.3 = 30%）
        self.min_equity_ratio: float = float(self.config.get("min_equity_ratio", 0.3))

        # ── ② 通貨ペア・Semaphore 初期化 ─────────────────
        self.symbol: str = self.config.get("target_symbol", "ETH-PERP")  # 例: "@123"

        self.dry_run: bool = bool(self.config.get("dry_run", False))
        self.last_side: str | None = None  # 直前に発注したサイド
        self.last_ts: float = 0.0  # 直前発注の Epoch 秒
        self.cooldown = self.config.get("cooldown_sec", 2)  # 連続発注抑制秒

        # ────────────────────────────────────────────────

        max_ops = int(self.config.get("max_order_per_sec", 3))  # 1 秒あたり発注上限
        self._sdk = sdk
        self.sem: asyncio.Semaphore = semaphore or asyncio.Semaphore(max_ops)
        self.max_order_per_sec = max_ops
        self.spread_th = Decimal(str(self.config["spread_threshold"]))
        # ── Runtime state ------------------------------------------------
        self.mid: Decimal = Decimal("0")  # 直近板ミッド
        self.funding_rate: Decimal = Decimal("0")
        self.pos_usd: Decimal = Decimal("0")
        self.last_side: str = ""
        self.last_ts: float = 0.0
        self.bid = self.ask = None

        # --- Risk metrics -----------------------------------------------
        self.max_daily_orders: int = int(self.config.get("max_daily_orders", 99999))
        self._order_count: int = 0
        self.max_drawdown_usd: Decimal = Decimal(
            str(self.config.get("max_drawdown_usd", 1e9))
        )
        self.drawdown_usd: Decimal = Decimal("0")
        self.enabled: bool = True  # funding ガード用フラグ
        # -----------------------------------------------------------------

        # 以降 (env 読み込み・SDK 初期化 …) は従来コードを続ける
        # ------------------------------------------------------------------
        # ── Strategy 専用ロガー ─────────────────────────

        self.logger = logging.getLogger(f"pfpl.{self.symbol}")
        if not self.logger.handlers:  # 重複防止
            h = logging.StreamHandler(sys.stdout)
            h.setLevel(logging.DEBUG)
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.logger.addHandler(h)
        self.logger.setLevel(logging.DEBUG)

        # -------------------------------------------------

        # ── 分析ログ (CSV) ───────────────────────────────

        csv_path = Path(f"logs/pfpl/strategy_{self.symbol}.csv")
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.alog = AnalysisLogger(csv_path)
        # -------------------------------------------------

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
        # --- meta 情報から tick / min_usd 決定 ──
        self.meta = self.exchange.info.meta()
        self.client = self.exchange
        # -----------------------------------------------------------------
        # 例: "@123"
        # ────────────────────────────────────────────────
        # min_usd
        if min_usd_cfg := self.config.get("min_usd"):
            self.min_usd = Decimal(str(min_usd_cfg))
            logger.info("min_usd override from config: USD %.2f", self.min_usd)
        else:
            min_usd_map: dict[str, str] = self.meta.get("minSizeUsd", {})
            self.min_usd = (
                Decimal(min_usd_map["ETH"]) if "ETH" in min_usd_map else Decimal("1")
            )
            if "ETH" not in min_usd_map:
                logger.warning("minSizeUsd missing ➜ fallback USD 1")

        # tick
        # --- tick ---------------------------------------------------------------
        # 取引所メタ情報に数量刻みがある場合はこちらを使用
        # --- tick ---------------------------------------------------------------
        # ペアごとに数量刻み (qty_tick) を上書きできる。無ければ meta → 最後は 0.001
        cfg_tick = self.config.get("qty_tick")  # 例: 0.0001
        meta_tick = self.meta.get("qtyTicks", {}).get(
            self.symbol.split("-")[0], "0.001"
        )
        self.tick = Decimal(str(cfg_tick or meta_tick))
        # -----------------------------------------------------------------------

        # -----------------------------------------------------------------------

        # ── Bot パラメータ ──────────────────────────────
        self.cooldown = float(self.config.get("cooldown_sec", 1.0))
        self.next_funding_ts: float | None = None  # 次回 Funding 実行時刻（Epoch 秒）

        self.order_usd = Decimal(self.config.get("order_usd", 10))
        self.max_pos = Decimal(self.config.get("max_position_usd", 100))
        self.fair_feed = config.get(
            "fair_feed", "activeAssetCtx"
        )  # default を activeAssetCtx に
        # strategy.py  __init__ 末尾
        self.th_buy = Decimal(
            str(self.config.get("threshold_buy", self.config["threshold"]))
        )
        self.th_sell = Decimal(
            str(self.config.get("threshold_sell", self.config["threshold"]))
        )

        self.spread_th_buy = Decimal(
            str(self.config.get("spread_threshold_buy", self.spread_th))
        )
        self.spread_th_sell = Decimal(
            str(self.config.get("spread_threshold_sell", self.spread_th))
        )
        self.th_buy = Decimal(
            str(self.config.get("threshold_buy", self.config["threshold"]))
        )
        self.th_sell = Decimal(
            str(self.config.get("threshold_sell", self.config["threshold"]))
        )

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
        self.drawdown_usd: Decimal = Decimal("0")  # ★ 追加（MaxDD トラッキング用）
        # ★ Funding Guard 用
        self.next_funding_ts: float | None = None  # 次回資金調達の UNIX 秒
        self._funding_pause: bool = False  # True なら売買停止

        # 非同期でポジション初期化
        try:
            asyncio.get_running_loop().create_task(self._refresh_position())
        except RuntimeError:
            pass  # pytest 収集時など、イベントループが無い場合

        # ───────────────────────────────────────────────
        self.logger.info(
            "CFG-DEBUG %s → keys=%s", self.symbol, list(self.config.keys())
        )
        self.logger.info("PARAM max_pos=%s", self.max_pos)

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

    def on_message(self, msg: dict[str, Any]) -> None:
        """各 WS メッセージを取り込み、mid / fair / funding を更新"""
        self.logger.debug(
            "WS ch=%s keys=%s", msg.get("channel"), list(msg.get("data", {}))[:3]
        )

        ch = msg.get("channel")

        # ── ① activeAssetCtx（または spot なら activeSpotAssetCtx）
        if ch in ("activeAssetCtx", "activeSpotAssetCtx"):
            # 自分のペア以外なら無視
            if msg["data"].get("coin") not in (self.symbol, self.symbol.split("-")[0]):
                return

            ctx = msg["data"]["ctx"]
            # フェア価格（マーク価格）
            self.fair = (
                Decimal(ctx.get("indexPx", ctx["markPx"])) + Decimal(ctx["oraclePx"])
            ) / 2
            # MidPx があればそのまま、無ければ bbo で更新する
            if "midPx" in ctx:
                self.mid = Decimal(ctx["midPx"])
            # Funding（パーペチュアルのみ）
            self.funding_rate = Decimal(ctx.get("funding", "0"))
            # 次回 Funding 時刻を保持
            if "nextFundingTimeMs" in ctx:
                self.next_funding_ts = ctx["nextFundingTimeMs"] / 1_000  # ミリ秒 → 秒

        # ── ② bbo  → bestBid / bestAsk から mid を都度計算
        elif ch == "bbo":
            # 自分のペア以外なら無視
            if msg["data"].get("coin") not in (self.symbol, self.symbol.split("-")[0]):
                return

            bid_px = Decimal(msg["data"]["bbo"][0]["px"])
            ask_px = Decimal(msg["data"]["bbo"][1]["px"])
            self.mid = (bid_px + ask_px) / 2
            self.bid = bid_px  # スプレッド判定用
            self.ask = ask_px  # ──────────
            self.logger.debug("[BBO] bid=%.2f ask=%.2f", bid_px, ask_px)

        # ── ③ 価格がそろったら評価ロジックへ
        if self.mid and self.fair:
            self.evaluate()
        else:
            self.logger.debug(
                "WAIT mid=%s fair=%s (skip evaluate)", self.mid, self.fair
            )

        # その他チャンネルは無視

        # fair が作れれば評価へ
        if self.mid and self.idx and self.ora:
            self.fair = (self.idx + self.ora) / 2  # ★ 平均で公正価格
            self.evaluate()
        # ★ fundingInfo 追加 ------------------------------
        if ch == "fundingInfo":
            # 例: {"channel":"fundingInfo","data":{"ETH-PERP":{"nextFundingTime":1720528800}}}
            info = msg["data"].get(self.symbol)
            if info:
                self.next_funding_ts = float(info["nextFundingTime"])
                logger.debug("fundingInfo: next @ %s", self.next_funding_ts)
        if self.symbol == "ETH-PERP":  # ← 1 ペアだけ見る
            logger.debug(
                "DBG mid=%s fair=%s keys=%s",
                self.mid,
                self.fair,
                list(self.mids.keys())[:5],
            )
        # ── ファイル: src/bots/pfpl/strategy.py ──────────────────
        # on_message() の最初か最後に 3 行コピペして保存
        if msg.get("channel") == "allMids":  # 受信は 1 回ごと
            logger.info(
                "MID KEYS: %s", list(msg["data"]["mids"].keys())[:50]
            )  # 先頭 50 件だけ表示

    # ────────────────────────────────────────────────────────
    # ------------------------------------------------------------------
    #  evaluate : mid / fair がそろったら毎回呼ばれる
    # ------------------------------------------------------------------
    def evaluate(self) -> None:
        # ── 0) 乖離符号を毎回記録 ─────────────────────────────
        if self.mid is not None and self.fair is not None:
            self.logger.debug("[DIFF_SIGN] %s", "+" if self.fair > self.mid else "-")

        # ── 1) パラメータ表示（1 回だけ） ──────────────────
        if not hasattr(self, "_debug_shown"):
            self._debug_shown = True
            self.logger.info(
                "PARAM order_usd=%s tick=%s min_usd=%s",
                self.order_usd,
                self.tick,
                self.min_usd,
            )

        # ── 2) bid / ask が来るまで待機 ─────────────────────
        if self.bid is None or self.ask is None:
            self.logger.debug("SKIP: bid/ask missing")
            return

        # ── 3) mid / fair が揃うまで待機 ────────────────────
        if self.mid is None or self.fair is None:
            self.logger.debug("SKIP: mid/fair missing")
            return
        side = "BUY" if self.fair < self.mid else "SELL"
        # ここで mid・fair OK
        self.logger.debug("[CHK-MIDFAIR] mid=%s fair=%s ok=True", self.mid, self.fair)

        # ── 4) 乖離計算 --------------------------------------------------
        abs_diff = abs(self.fair - self.mid)
        pct_diff = abs_diff / self.mid * 100
        th_abs = self.th_buy if side == "BUY" else self.th_sell
        th_pct = Decimal(str(self.config.get("threshold_pct", "0.0001")))
        mode = self.config.get("mode", "either")  # abs / pct / both / either
        self.logger.debug("[DIFF] %.6f", th_abs)

        # 置き換えコード
        skip = (
            (mode == "abs" and abs_diff < th_abs)
            or (mode == "pct" and pct_diff < th_pct)
            or (mode == "both" and (abs_diff < th_abs or pct_diff < th_pct))
            or (mode == "either" and (abs_diff < th_abs and pct_diff < th_pct))
        )

        self.logger.debug("[CHK-THRESH] abs=%s pct=%s skip=%s", th_abs, th_pct, skip)
        if skip:
            return

        # ── 5) スプレッドガード ------------------------------------------
        th_spread = self.spread_th_buy if side == "BUY" else self.spread_th_sell
        blocked = th_spread > th_spread
        self.logger.debug(
            "[CHK-SPREAD] spread=%.4f th=%.4f blocked=%s", th_spread, th_spread, blocked
        )
        if blocked:
            return

        # ── 6) Funding 直前クローズ --------------------------------------
        now = time.time()
        if (
            getattr(self, "next_funding_ts", None)
            and now > self.next_funding_ts - self.funding_close_buffer_secs
        ):
            if self.pos_usd:
                self.logger.info("FUNDING_CLOSE pos_usd=%s - closing all", self.pos_usd)
                asyncio.create_task(self._close_all())
            return

        # ── 7) Equity ratio ガード --------------------------------------
        max_ratio = float(self.config.get("max_equity_ratio", 1.0))
        cur_ratio = self._get_equity_ratio()
        if cur_ratio > max_ratio:
            self.logger.warning(
                "EQUITY-SKIP ratio=%.3f > max=%.3f", cur_ratio, max_ratio
            )
            return

        # ── 8) クールダウン & ポジション上限 -----------------------------
        if now - self.last_ts < self.cooldown:
            return
        if abs(self.pos_usd) >= self.max_pos:
            return

        # ── 9) 発注ロジック ---------------------------------------------
        side = "BUY" if self.fair < self.mid else "SELL"
        raw_sz = Decimal(str(self.order_usd)) / Decimal(str(self.mid))
        size = raw_sz.quantize(self.tick, rounding=ROUND_UP)
        usd_val = size * self.mid

        if usd_val < self.min_usd:
            self.logger.debug("SIZE_SKIP %s < min_usd %s", usd_val, self.min_usd)
            return

        if (
            abs(self.pos_usd + (size * self.mid if side == "BUY" else -size * self.mid))
            > self.max_pos
        ):
            self.logger.info(
                "POS_SKIP pos_usd=%s >= max_pos=%s - order blocked",
                self.pos_usd,
                self.max_pos,
            )
            return

        # ── 10) 発注（dry-run or live） ----------------------------------
        if self.dry_run:
            delta = size * self.mid if side == "BUY" else -size * self.mid
            self.pos_usd += delta
            self.logger.info("[DRY-RUN] %s %.4f %s", side, float(size), self.symbol)
        else:
            asyncio.create_task(self.place_order(side, float(size)))

        self.alog.log_trade(
            symbol=self.symbol,
            side=side,
            size=float(size),
            price=float(self.mid),
            reason="signal",
        )
        self.logger.info("TASK-SCHED side=%s size=%s", side, size)

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
        self.logger.info("PLACE_CALL side=%s size=%s", side, size)

        """IOC で即時約定、失敗時リトライ付き"""
        is_buy = side == "BUY"
        now = time.time()
        if side == self.last_side and now - self.last_ts < self.cooldown:
            logger.info(
                "COOLDOWN‑SKIP %.2fs < %ds (side=%s)",
                now - self.last_ts,
                self.cooldown,
                side,
            )
            return  # 連続発注を抑制

        # ── Dry-run ───────────────────
        if self.dry_run:

            logger.info("[DRY-RUN] %s %.4f %s", side, size, self.symbol)
            self.last_ts = time.time()
            self.last_side = side
            self.alog.log_trade(
                symbol=self.symbol,
                side=side,
                size=size,
                price=float(self.mid),
                reason="DRY",
            )
            self.logger.info("ALOG-WRITE dry %s %.4f %s", side, size, self.symbol)

            return 1.0
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
                        coin=self.symbol.split("-")[0],
                        is_buy=is_buy,
                        sz=float(size),
                        limit_px=limit_px,
                        order_type={"limit": {"tif": "Ioc"}},  # IOC 指定
                        reduce_only=False,
                    )
                    logger.info("ORDER OK %s try=%d → %s", self.symbol, attempt, resp)
                    asyncio.create_task(
                        discord_notify(
                            f"✅ {side} {size:.4f} {self.symbol} @ {limit_px}"
                        )
                    )

                    self.alog.log_trade(
                        symbol=self.symbol,
                        side=side,
                        size=size,
                        price=float(limit_px or self.mid),
                        reason="ENTRY",
                    )

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
                        asyncio.create_task(
                            discord_notify(f"❌ ORDER FAIL {self.symbol}: {exc}")
                        )

                    else:
                        await anyio.sleep(0.5)

    def _sign(self, payload: dict[str, Any]) -> str:
        """API Wallet Secret で HMAC-SHA256 署名（例）"""
        msg = json.dumps(payload, separators=(",", ":")).encode()
        return hmac.new(self.secret.encode(), msg, hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------ limits
    def _check_limits(self) -> bool:
        # dry-run 時はガードを全部 PASS する
        if self.dry_run:
            return True
        """証拠金率・日次発注数・建玉・DD をまとめて判定"""
        # ── 0) 証拠金率 --------------------------------------------------
        try:
            ratio = self._get_equity_ratio()  # ← API 取得
        except Exception as e:
            # 失敗したら PASS（ratio=1.0）にフォールバック
            self.logger.warning("equity-ratio fetch failed: %s", e)
            asyncio.create_task(discord_notify(f"‼️ Task exception: {e}"))

            ratio = 0.0

        if ratio < self.min_equity_ratio:
            self.logger.warning(
                "equity-ratio %.3f < %.3f → skip", ratio, self.min_equity_ratio
            )
            return False

        # ── 1) 日次発注上限 ----------------------------------------------
        today = datetime.utcnow().date()
        if today != self._start_day:  # 日付が変わったらリセット
            self._start_day = today
            self._order_count = 0

        if self._order_count >= self.max_daily_orders:
            self.logger.warning(
                "daily order-limit %d reached → skip", self.max_daily_orders
            )
            return False

        # ── 2) 建玉上限 ---------------------------------------------------
        if abs(self.pos_usd) >= self.max_pos:
            self.logger.warning("position limit %.2f USD reached → skip", self.max_pos)
            return False

        # ── 3) ドローダウン上限 ------------------------------------------
        if self.drawdown_usd >= self.max_drawdown_usd:
            self.logger.warning(
                "drawdown limit %.2f USD reached → skip", self.max_drawdown_usd
            )
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
        logger.info(
            "CHK_FUNDWIN in_window=%s next_ts=%s now=%s",
            in_window,
            self.next_funding_ts,
            now,
        )

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
        acct = self.exchange.info.user_state(self.account)  # 全ポジション取得
        pos = next(
            (
                p
                for p in acct["assetPositions"]
                if p["asset"] == self.symbol.split("-")[0]
            ),
            None,
        )
        if not pos or float(pos["positionValue"]) == 0:
            return  # 建玉なしなら何もせず抜ける

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

    # ------------------------------------------------------------------
    # Equity helper
    # ───────────────────────────────────── equity guard
    def _get_equity_ratio(self) -> float:
        """
        equityUsd ÷ risk.maxPositionUsd を返す。
        HTTPClient 実装に合わせて account_info / portfolio / user_state の
        いずれかがあれば利用する。取れなければ 0.0 を返しガード無効。
        """
        try:
            # ─ 1) メソッド名を順番に探す ─
            if hasattr(self._sdk, "account_info"):
                info = self._sdk.account_info()
            elif hasattr(self._sdk, "portfolio"):
                info = self._sdk.portfolio()
            elif hasattr(self._sdk, "user_state"):
                info = self._sdk.user_state()
            else:  # どれも無ければ取得不能
                return 0.0

            # ─ 2) レスポンスから必要フィールドを読む ─
            eq_usd = float(info.get("equityUsd", 0))
            max_pos = float(info.get("risk", {}).get("maxPositionUsd", 0))
            return eq_usd / max_pos if max_pos else 0.0

        except Exception as e:  # noqa: BLE001
            self.logger.debug("equity-ratio fetch failed: %s - fallback 0.0", e)
            return 0.0

    # ------------------------------------------------------------------
    # Universe helper
    # ------------------------------------------------------------------
    def _get_universe(self) -> list[dict]:
        if not self._universe:
            self._universe = self.meta.get("universe", [])
        return self._universe
