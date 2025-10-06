# src/bots/pfpl/strategy.py
from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone  # ← 追加
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from pathlib import Path
from typing import Any, cast

import anyio
from hl_core.config import load_settings
from hl_core.utils.logger import create_csv_formatter, setup_logger, get_logger
# 既存 import 群の最後あたりに追加
from hyperliquid.exchange import Exchange

# 目的: 取引API(hl_core.api)のログレベルをDEBUGに上げ、注文送信の詳細ログを必ず出す
logging.getLogger("hl_core.api").setLevel(logging.DEBUG)

try:  # pragma: no cover - PyYAML may be absent in the test environment
    import yaml  # type: ignore
except ImportError as exc:  # noqa: F401 - surface missing PyYAML explicitly
    raise RuntimeError(
        "PyYAML が見つかりません。pfpl ボットの設定ファイルを読み込むには "
        "`pip install pyyaml` などで PyYAML をインストールしてください。"
    ) from exc

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


logger = get_logger(__name__)



# 役割: この関数は PFPL 戦略ロガーの親への伝播を止め、二重ログ（runner.csv / pfpl.csv など）を防ぎます
def _lock_strategy_logger_to_self(target: logging.Logger) -> None:
    """戦略ロガーのログが親ロガーへ伝播しないようにする（重複出力の抑止）。"""

    target.propagate = False


_lock_strategy_logger_to_self(logger)


def _maybe_enable_test_propagation() -> None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        # pytest では caplog が root ロガーをフックするため、伝播を許可して
        # 既存のテストでログを捕捉できるようにする
        logger.propagate = True


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """設定値を真偽値へ変換するヘルパー。"""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "0", "false", "off", "no"}:
            return False
        if normalized in {"1", "true", "on", "yes"}:
            return True
        # それ以外の文字列は Python の bool キャストに合わせる
        return bool(normalized)
    return bool(value)


class PFPLStrategy:
    """Price-Fair-Price-Lag bot"""

    # 何をする関数か:
    # - mid と fair の乖離（絶対値/率）を計算
    # - threshold / threshold_pct / spread_threshold の合否を判定
    # - cooldown / 最大ポジション / 最小発注額 / funding guard の合否を判定
    # - 上記の内訳を1行の DEBUG ログに要約出力（発注はしない）
    # - 後段の判定/発注で再利用できる dict を返す
    def _debug_evaluate_signal(
        self,
        *,
        mid_px: float,
        fair_px: float,
        order_usd: float,
        pos_usd: float,
        last_order_ts: float | None,
        funding_blocked: bool,
    ) -> dict:
        _maybe_enable_test_propagation()
        logger = getattr(self, "logger", None) or getattr(self, "log", None) or logging.getLogger(__name__)
        now = time.time()

        diff_abs = mid_px - fair_px
        diff_pct = (diff_abs / fair_px) if fair_px else 0.0

        thr_abs = getattr(self, "threshold", 0.0)
        thr_pct = getattr(self, "threshold_pct", 0.0)
        spread_thr = getattr(self, "spread_threshold", 0.0)
        cooldown = getattr(self, "cooldown_sec", 0.0)
        max_pos = getattr(self, "max_position_usd", float("inf"))
        min_usd = getattr(self, "min_usd", 0.0)

        cooldown_ok = (now - (last_order_ts or 0.0)) >= cooldown
        abs_ok = (abs(diff_abs) >= thr_abs) if thr_abs else False
        pct_ok = (abs(diff_pct) >= thr_pct) if thr_pct else False
        spread_ok = (abs(diff_abs) >= spread_thr) if spread_thr else True
        pos_ok = (abs(pos_usd) + order_usd) <= max_pos
        notional_ok = order_usd >= min_usd
        funding_ok = not funding_blocked

        # 方向の示唆（情報表示のみ）
        want_long = (diff_abs <= -thr_abs) or (diff_pct <= -thr_pct if thr_pct else False)
        want_short = (diff_abs >= thr_abs) or (diff_pct >= thr_pct if thr_pct else False)

        logger.debug(
            "decision mid=%.2f fair=%.2f d_abs=%+.4f d_pct=%+.5f | "
            "abs>=%.4f:%s pct>=%.5f:%s spread>=%.4f:%s | "
            "cooldown_ok=%s pos_ok=%s notional_ok=%s funding_ok=%s | "
            "long=%s short=%s",
            mid_px,
            fair_px,
            diff_abs,
            diff_pct,
            thr_abs,
            abs_ok,
            thr_pct,
            pct_ok,
            spread_thr,
            spread_ok,
            cooldown_ok,
            pos_ok,
            notional_ok,
            funding_ok,
            want_long,
            want_short,
        )

        return {
            "diff_abs": diff_abs,
            "diff_pct": diff_pct,
            "abs_ok": abs_ok,
            "pct_ok": pct_ok,
            "spread_ok": spread_ok,
            "cooldown_ok": cooldown_ok,
            "pos_ok": pos_ok,
            "notional_ok": notional_ok,
            "funding_ok": funding_ok,
            "want_long": want_long,
            "want_short": want_short,
            "ts": now,
        }

    # ← シグネチャはそのまま
    _LOGGER_INITIALISED = False
    _FILE_HANDLERS: set[str] = set()

    def __init__(
        self, *, config: dict[str, Any], semaphore: asyncio.Semaphore | None = None
    ):
        _maybe_enable_test_propagation()
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
        self.config = {**config, **yaml_conf}
        funding_guard_cfg = self.config.get("funding_guard", {})
        if not isinstance(funding_guard_cfg, dict):
            funding_guard_cfg = {}
        self.funding_guard_enabled: bool = _coerce_bool(
            funding_guard_cfg.get("enabled"), default=True
        )
        self.funding_guard_buffer_sec: int = int(
            funding_guard_cfg.get("buffer_sec", 300)
        )
        self.funding_guard_reenter_sec: int = int(
            funding_guard_cfg.get("reenter_sec", 120)
        )
        legacy_close_buffer = self.config.get("funding_close_buffer_secs", 120)
        self.funding_close_buffer_secs: int = int(
            funding_guard_cfg.get("buffer_sec", legacy_close_buffer)
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
        self.position_refresh_interval = float(
            self.config.get("position_refresh_interval_sec", 5.0)
        )
        self._position_refresh_task: asyncio.Task | None = None
        # ★ Funding Guard 用
        self.next_funding_ts: float | None = None  # 直近 funding 予定の UNIX 秒
        self._funding_pause: bool = False  # True なら売買停止中
        # 非同期でポジション初期化
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None  # pytest 収集時など、イベントループが無い場合

        if loop is not None:
            loop.create_task(self._refresh_position())
            if self.position_refresh_interval > 0:
                self._position_refresh_task = loop.create_task(
                    self._position_refresh_loop()
                )

        # ─── ここから追加（ロガーをペアごとのファイルへも出力）────
        handler_filename = f"strategy_{self.symbol}.csv"
        module_logger = logger
        existing_handler = next(
            (
                h
                for h in module_logger.handlers
                if isinstance(h, logging.FileHandler)
                and getattr(h, "baseFilename", "").endswith(handler_filename)
            ),
            None,
        )

        if existing_handler is None:
            handler = logging.FileHandler(handler_filename, encoding="utf-8")
            handler.setFormatter(create_csv_formatter(include_logger_name=False))
            module_logger.addHandler(handler)

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

    async def _position_refresh_loop(self) -> None:
        """Periodically refresh position to capture passive fills."""
        interval = self.position_refresh_interval
        if interval <= 0:
            return

        while True:
            await self._refresh_position()
            try:
                await anyio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("position refresh sleep failed: %s", exc)
                await asyncio.sleep(max(interval, 1))

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
            if price_val is None:
                price_val = prices.get(self.symbol)
            new_idx = Decimal(str(price_val)) if price_val is not None else None
            if new_idx != self.idx:
                self.idx = new_idx
                fair_inputs_changed = True
                if uses_index:
                    should_eval = True
        elif ch == "oraclePrices":  # オラクル価格
            prices = (msg.get("data") or {}).get("prices") or {}
            price_val = prices.get(self.base_coin)
            if price_val is None:
                price_val = prices.get(self.symbol)
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
        import logging

        _logger = getattr(self, "logger", logging.getLogger(__name__))
        try:
            _thr = getattr(self, "threshold", None)
            _pct = getattr(self, "threshold_pct", None)
            _spr = getattr(self, "spread_threshold", None)
            _mid = locals().get("mid", locals().get("mid_px", getattr(self, "mid", None)))
            _fair = locals().get("fair", locals().get("fair_px", getattr(self, "fair", None)))
            _diff = None if (_mid is None or _fair is None) else (_mid - _fair)
            _logger.debug(
                "DECISION_SNAPSHOT mid=%s fair=%s diff=%s | thr=%.6f spr=%.6f pct=%.6f",
                None if _mid is None else f"{_mid:.6f}",
                None if _fair is None else f"{_fair:.6f}",
                None if _diff is None else f"{_diff:.6f}",
                0.0 if _thr is None else float(_thr),
                0.0 if _spr is None else float(_spr),
                0.0 if _pct is None else float(_pct),
            )
        except Exception as _e:
            _logger.debug("DECISION_SNAPSHOT_UNAVAILABLE reason=%r", _e)

        _maybe_enable_test_propagation()
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
            fallback_px = limit_px
            if fallback_px is None:
                if mid_value is not None:
                    try:
                        fallback_px = self._price_with_offset(float(mid_value), side)
                    except (TypeError, ValueError):  # pragma: no cover - defensive
                        fallback_px = None
                if fallback_px is None:
                    logger.warning(
                        "market order requested but no price reference available; skip"
                    )
                    return
                logger.debug("market order fallback limit_px=%s", fallback_px)
            order_type_payload = {"market": {}}
            limit_px = fallback_px
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
                    asyncio.create_task(self._refresh_position())
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
        if not self.funding_guard_enabled:
            if self._funding_pause:
                self._funding_pause = False
            return True
        if self.next_funding_ts is None:
            return True  # fundingInfo 未取得なら通常運転

        now = time.time()
        before = self.funding_guard_buffer_sec
        after = self.funding_guard_reenter_sec

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
        if not self.funding_guard_enabled:
            return False
        next_ts = getattr(self, "next_funding_ts", None)
        if not next_ts:
            return False
        return now_ts > next_ts - self.funding_guard_buffer_sec

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
            fallback_px: float | None = None
            mid_snapshot = self.mid
            if mid_snapshot is not None:
                try:
                    fallback_px = self._price_with_offset(float(mid_snapshot), close_side)
                except (TypeError, ValueError):  # pragma: no cover - defensive
                    fallback_px = None
            if fallback_px is None:
                try:
                    entry_px = perp_pos["position"].get("entryPx")
                except Exception:  # pragma: no cover - defensive
                    entry_px = None
                if entry_px is not None:
                    try:
                        fallback_px = self._price_with_offset(float(entry_px), close_side)
                    except (TypeError, ValueError):  # pragma: no cover - defensive
                        fallback_px = None
            await self.place_order(
                side=close_side,
                size=float(abs(sz)),
                order_type="market",
                limit_px=fallback_px,
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
