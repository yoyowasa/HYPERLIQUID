# src/bots/pfpl/strategy.py
from __future__ import annotations
import os
import logging
from typing import Any, cast
import asyncio
import hmac
import hashlib
import json
import time
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from hl_core.config import load_settings
from hl_core.utils.logger import setup_logger
from pathlib import Path

try:  # pragma: no cover - PyYAML may be absent in the test environment
    import yaml  # type: ignore
except Exception:  # noqa: F401 - fallback when PyYAML isn't installed
    import json as _json

    class _YAMLModule:  # minimal shim with safe_load
        @staticmethod
        def safe_load(stream: str):  # type: ignore[override]
            try:
                return _json.loads(stream)
            except Exception:
                return {}

    yaml = _YAMLModule()  # type: ignore

import anyio
from datetime import datetime, timezone  # ← 追加

# 既存 import 群の最後あたりに追加
from hyperliquid.exchange import Exchange

try:  # pragma: no cover - eth_account is optional for tests
    from eth_account.account import Account  # type: ignore
except Exception:  # noqa: F401 - fallback when eth_account isn't installed

    class Account:  # type: ignore
        @staticmethod
        def from_key(key: str):
            class _Wallet:
                def __init__(self, key: str) -> None:
                    self.key = key

            return _Wallet(key)


logger = logging.getLogger(__name__)


class PFPLStrategy:
    """Price-Fair-Price-Lag bot"""

    # ← シグネチャはそのまま
    _LOGGER_INITIALISED = False
    _FILE_HANDLERS: set[str] = set()

    def __init__(
        self, *, config: dict[str, Any], semaphore: asyncio.Semaphore | None = None
    ):
        if not PFPLStrategy._LOGGER_INITIALISED:
            setup_logger(bot_name="pfpl")
            PFPLStrategy._LOGGER_INITIALISED = True
        # ── ① YAML と CLI のマージ ───────────────────────
        yml_path = Path(__file__).with_name("config.yaml")
        yaml_conf: dict[str, Any] = {}
        if yml_path.exists():
            with yml_path.open(encoding="utf-8") as f:
                raw_conf = f.read()
            yaml_conf = yaml.safe_load(raw_conf) or {}
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
        sym_parts = self.symbol.split("-", 1)
        self.base_coin: str = sym_parts[0] if sym_parts else self.symbol

        max_ops = int(self.config.get("max_order_per_sec", 3))  # 1 秒あたり発注上限
        self.sem: asyncio.Semaphore = semaphore or asyncio.Semaphore(max_ops)

        # 以降 (env 読み込み・SDK 初期化 …) は従来コードを続ける
        # ------------------------------------------------------------------

        # ── 環境変数キー ────────────────────────────────
        settings = load_settings()

        def _first_nonempty(*values: Any) -> str | None:
            for value in values:
                if value is None:
                    continue
                if not isinstance(value, str):
                    candidate = str(value)
                else:
                    candidate = value
                candidate = candidate.strip()
                if candidate:
                    return candidate
            return None

        account = _first_nonempty(
            self.config.get("account_address"),
            settings.account_address,
            os.getenv("HL_ACCOUNT_ADDRESS"),
            os.getenv("HL_ACCOUNT_ADDR"),
        )
        secret = _first_nonempty(
            self.config.get("private_key"),
            settings.private_key,
            os.getenv("HL_PRIVATE_KEY"),
            os.getenv("HL_API_SECRET"),
        )

        missing_parts: list[str] = []
        if not account:
            missing_parts.append(
                "account address (set HL_ACCOUNT_ADDRESS or legacy HL_ACCOUNT_ADDR)"
            )
        if not secret:
            missing_parts.append(
                "private key (set HL_PRIVATE_KEY or legacy HL_API_SECRET)"
            )
        if missing_parts:
            raise ValueError(
                "Missing Hyperliquid credentials: " + "; ".join(missing_parts)
            )

        account = cast(str, account)
        secret = cast(str, secret)
        self.account: str = account
        self.secret: str = secret

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
            min_usd_raw = min_usd_map.get(self.base_coin)
            if min_usd_raw is not None:
                self.min_usd = Decimal(str(min_usd_raw))
                logger.info(
                    "min_usd from meta for %s: USD %.2f", self.base_coin, self.min_usd
                )
            else:
                self.min_usd = Decimal("1")
                logger.warning(
                    "minSizeUsd missing for %s ➜ fallback USD 1", self.base_coin
                )

        # tick
        uni_entry = next(u for u in meta["universe"] if u["name"] == self.base_coin)
        tick_raw = uni_entry.get("pxTick") or uni_entry.get("pxTickSize", "0.01")
        self.tick = Decimal(str(tick_raw))
        logger.info("pxTick for %s: %s", self.base_coin, self.tick)

        qty_tick_val: Decimal | None = None
        qty_tick_raw = uni_entry.get("qtyTick")
        if qty_tick_raw is not None:
            try:
                qty_tick_val = Decimal(str(qty_tick_raw))
            except Exception:  # pragma: no cover - defensive parsing
                qty_tick_val = None
        if qty_tick_val is None:
            sz_decimals = uni_entry.get("szDecimals")
            try:
                if sz_decimals is not None:
                    qty_tick_val = Decimal("1").scaleb(-int(sz_decimals))
            except Exception:  # pragma: no cover - defensive parsing
                qty_tick_val = None
        if qty_tick_val is None or qty_tick_val <= 0:
            self.qty_tick = Decimal("0.0001")
            logger.warning(
                "qtyTick missing for %s ➜ fallback %s",
                self.base_coin,
                self.qty_tick,
            )
        else:
            self.qty_tick = qty_tick_val
            logger.info("qtyTick for %s: %s", self.base_coin, self.qty_tick)

        # ── Bot パラメータ ──────────────────────────────
        self.cooldown = float(self.config.get("cooldown_sec", 1.0))
        self.order_usd = Decimal(self.config.get("order_usd", 10))
        self.dry_run = bool(self.config.get("dry_run"))
        self.max_pos = Decimal(self.config.get("max_position_usd", 100))
        self.fair_feed = self.config.get("fair_feed", "indexPrices")
        self.max_daily_orders = int(self.config.get("max_daily_orders", 500))
        self._order_count = 0
        self._start_day = datetime.now(timezone.utc).date()
        self.enabled = True
        # ── フィード保持用 -------------------------------------------------
        self.mid: Decimal | None = None  # 板 Mid (@1)
        self.idx: Decimal | None = None  # indexPrices
        self.ora: Decimal | None = None  # oraclePrices
        self.fair: Decimal | None = None  # 平均した公正価格

        # ── 内部ステート ────────────────────────────────
        self.last_side: str | None = None
        self.last_ts: float = 0.0
        self.pos_usd = Decimal("0")
        # ★ Funding Guard 用
        self.next_funding_ts: float | None = None  # 直近 funding 予定の UNIX 秒
        self._funding_pause: bool = False  # True なら売買停止中
        # 非同期でポジション初期化
        try:
            asyncio.get_running_loop().create_task(self._refresh_position())
        except RuntimeError:
            pass  # pytest 収集時など、イベントループが無い場合

        # ─── ここから追加（ロガーをペアごとのファイルへも出力）────
        if self.symbol not in PFPLStrategy._FILE_HANDLERS:
            h = logging.FileHandler(f"strategy_{self.symbol}.log", encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(h)
            PFPLStrategy._FILE_HANDLERS.add(self.symbol)

        logger.info("PFPLStrategy initialised with %s", self.config)

    # ── src/bots/pfpl/strategy.py ──
    async def _refresh_position(self) -> None:
        """
        現在の建玉 USD を self.pos_usd に反映。
        perpPositions が無い口座でも落ちない。
        """
        try:
            state = self.exchange.info.user_state(self.account)

            # ―― 対象コインの perp 建玉を抽出（無い場合は None）
            perp_pos = next(
                (
                    p
                    for p in state.get("perpPositions", [])  # ← 🔑 get(..., [])
                    if p["position"]["coin"] == self.base_coin
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
        feed = self.fair_feed
        combined_feed = feed not in {"indexPrices", "oraclePrices"}
        uses_index = feed == "indexPrices" or combined_feed
        uses_oracle = feed == "oraclePrices" or combined_feed

        should_eval = False
        fair_inputs_changed = False

        if ch == "allMids":  # 板 mid 群
            mids = (msg.get("data") or {}).get("mids") or {}

            mid_key: str | None = None
            mid_raw = None
            for candidate in (self.base_coin, self.symbol):
                if candidate and candidate in mids:
                    mid_raw = mids[candidate]
                    mid_key = candidate
                    break

            if mid_raw is None:
                logger.debug(
                    "allMids: waiting for mid for %s (base=%s)",
                    self.symbol,
                    self.base_coin,
                )
                return

            try:
                new_mid = Decimal(str(mid_raw))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "allMids: failed to parse mid %r for %s: %s",
                    mid_raw,
                    mid_key or self.symbol,
                    exc,
                )
                return

            if new_mid != self.mid:
                self.mid = new_mid
                logger.debug("allMids: mid[%s]=%s", mid_key, self.mid)
                should_eval = True
        elif ch == "indexPrices":  # インデックス価格
            prices = (msg.get("data") or {}).get("prices") or {}
            price_val = prices.get(self.base_coin)
            new_idx = Decimal(str(price_val)) if price_val is not None else None
            if new_idx != self.idx:
                self.idx = new_idx
                fair_inputs_changed = True
                if uses_index:
                    should_eval = True
        elif ch == "oraclePrices":  # オラクル価格
            prices = (msg.get("data") or {}).get("prices") or {}
            price_val = prices.get(self.base_coin)
            new_ora = Decimal(str(price_val)) if price_val is not None else None
            if new_ora != self.ora:
                self.ora = new_ora
                fair_inputs_changed = True
                if uses_oracle:
                    should_eval = True
        elif ch == "fundingInfo":
            data = msg.get("data", {})
            next_ts = data.get("nextFundingTime") if isinstance(data, dict) else None
            if next_ts is None and isinstance(data, dict):
                info = data.get(self.symbol)
                if isinstance(info, dict):
                    next_ts = info.get("nextFundingTime")
            if next_ts is None and isinstance(data, dict):
                base_info = data.get(self.base_coin)
                if isinstance(base_info, dict):
                    next_ts = base_info.get("nextFundingTime")
            if next_ts is not None:
                self.next_funding_ts = float(next_ts)
                logger.debug("fundingInfo: next @ %s", self.next_funding_ts)

        if fair_inputs_changed:
            self._update_fair()

        if should_eval and self.mid is not None and self.fair is not None:
            self.evaluate()

    def _update_fair(self) -> None:
        feed = self.fair_feed
        if feed == "indexPrices":
            self.fair = self.idx
        elif feed == "oraclePrices":
            self.fair = self.ora
        else:
            if self.idx is not None and self.ora is not None:
                self.fair = (self.idx + self.ora) / Decimal("2")
            else:
                self.fair = None

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
        mid = self.mid
        fair = self.fair
        if mid is None or fair is None:
            return  # データが揃っていない

        diff = fair - mid  # USD 差（符号付き）
        diff_pct = diff / mid * Decimal("100")  # 乖離率 %（符号付き）
        abs_diff = abs(diff)
        pct_diff = abs(diff_pct)

        # ④ 閾値判定
        th_abs = Decimal(str(self.config.get("threshold", "1.0")))  # USD
        th_pct = Decimal(str(self.config.get("threshold_pct", "0.05")))  # %
        mode = self.config.get("mode", "both")  # both / either

        logger.debug(
            "signal: diff=%+.6f diff_pct=%+.6f thr=%.6f thr_pct=%.6f pos_usd=%+.2f order_usd=%.2f dry_run=%s",
            diff,
            diff_pct,
            th_abs,
            th_pct,
            self.pos_usd,
            self.order_usd,
            self.dry_run,
        )

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
        side = "BUY" if fair > mid else "SELL"

        # ⑥ 連続同方向防止
        if side == self.last_side and now - self.last_ts < self.cooldown:
            return

        # ⑦ 発注サイズ計算
        raw_size = self.order_usd / mid
        try:
            size = raw_size.quantize(self.qty_tick, rounding=ROUND_DOWN)
        except InvalidOperation:
            logger.error(
                "quantize failed for raw size %s with qty_tick %s",
                raw_size,
                self.qty_tick,
            )
            return
        if size <= 0:
            logger.debug(
                "size %.6f quantized to zero with tick %s → skip",
                raw_size,
                self.qty_tick,
            )
            return
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
        reduce_only: bool = False,
        time_in_force: str | None = None,
        **kwargs,
    ) -> None:
        """IOC で即時約定、失敗時リトライ付き"""
        is_buy = side == "BUY"
        mid_value = self.mid

        tif = time_in_force
        if "time_in_force" in kwargs and tif is None:
            tif = kwargs.pop("time_in_force")
        if "tif" in kwargs and tif is None:
            tif = kwargs.pop("tif")
        ioc_requested = kwargs.pop("ioc", None)

        order_type_payload: dict[str, Any]

        # --- eps_pct を適用した価格補正 -------------------------------
        if order_type == "limit":
            if limit_px is None:
                if mid_value is None:
                    logger.warning("mid price unavailable; skip order placement")
                    return
                limit_px = self._price_with_offset(float(mid_value), side)

            limit_body: dict[str, Any] = {}
            if tif is not None:
                limit_body["tif"] = tif
            else:
                use_ioc = True if ioc_requested is None else bool(ioc_requested)
                if use_ioc:
                    limit_body["tif"] = "Ioc"

            order_type_payload = {"limit": limit_body}
        elif order_type == "market":
            order_type_payload = {"market": {}}
            limit_px = None
        else:
            logger.error("unsupported order_type=%s", order_type)
            return

        try:
            size_dec = Decimal(str(size)).quantize(self.qty_tick, rounding=ROUND_DOWN)
        except InvalidOperation:
            logger.error(
                "place_order: quantize failed for size %s with qty_tick %s",
                size,
                self.qty_tick,
            )
            return
        if size_dec <= 0:
            logger.debug(
                "place_order: size %s → %s after quantize %s → skip",
                size,
                size_dec,
                self.qty_tick,
            )
            return

        order_kwargs: dict[str, Any] = {
            "coin": self.base_coin,
            "is_buy": is_buy,
            "sz": float(size_dec),
            "order_type": order_type_payload,
            "reduce_only": reduce_only,
            **kwargs,
        }
        if limit_px is not None:
            order_kwargs["limit_px"] = limit_px

        # ── Dry-run ───────────────────
        if self.dry_run:
            logger.info("[DRY-RUN] %s %.4f %s", side, size, self.symbol)
            logger.info("[DRY-RUN] payload=%s", order_kwargs)
            self.last_ts = time.time()
            self.last_side = side
            return
        # ──────────────────────────────

        async with self.sem:  # 1 秒あたり発注制御
            MAX_RETRY = 3
            order_fn = getattr(self.exchange, "order", None)
            if not callable(order_fn):
                raise AttributeError("exchange.order is not callable")
            for attempt in range(1, MAX_RETRY + 1):
                try:
                    logger.info(
                        "ORDER_SIGNAL symbol=%s side=%s qty=%s price=%s extra=%s",
                        locals().get("symbol"),
                        locals().get("side"),
                        locals().get("qty", locals().get("size")),
                        locals().get("price"),
                        {"mid": locals().get("mid"), "reason": locals().get("reason")},
                    )
                    resp = await asyncio.to_thread(order_fn, **order_kwargs)
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
        """日次の発注数と建玉制限を超えていないか確認"""
        today = datetime.now(timezone.utc).date()
        if today != self._start_day:  # 日付が変わったらリセット
            self._start_day = today
            self._order_count = 0

        if self._order_count >= self.max_daily_orders:
            logger.warning("daily order-limit reached → trading disabled")
            return False

        if abs(self.pos_usd) >= self.max_pos:
            logger.warning("position limit %.2f USD reached", self.max_pos)
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
        try:
            state = self.exchange.info.user_state(self.account)
            coin = self.base_coin
            perp_pos = next(
                (
                    p
                    for p in state.get("perpPositions", [])
                    if p["position"]["coin"] == coin
                ),
                None,
            )
            if not perp_pos:
                return
            sz = Decimal(perp_pos["position"]["sz"])
            if sz == 0:
                return  # 持ち高なし
            close_side = "SELL" if sz > 0 else "BUY"
            await self.place_order(
                side=close_side,
                size=float(abs(sz)),
                order_type="market",
                reduce_only=True,
                comment="auto‑close‑before‑funding",
            )
            logger.info(
                "⚡ Funding close: %s %s @ %s (buffer %s s)",
                close_side,
                abs(sz),
                self.symbol,
                self.funding_close_buffer_secs,
            )
        except Exception as exc:
            logger.error("close_all_positions failed: %s", exc)

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


def log_order_decision(
    logger,
    symbol: str,
    side: str,
    qty: float,
    price: float | None,
    reason: str,
    will_send: bool,
) -> None:
    """この関数がすること: 発注する/しない の判定結果と理由を1行でログに残す。送るならINFO、送らないならDEBUG。"""
    level = logging.INFO if will_send else logging.DEBUG
    logger.log(
        level,
        "order_decision symbol=%s side=%s qty=%s price=%s will_send=%s reason=%s",
        symbol,
        side,
        qty,
        price,
        will_send,
        reason,
    )
