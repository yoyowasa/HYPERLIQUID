# src/bots/pfpl/strategy.py
from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
# 役割: 実行中に「どの行でログが出たか(lineno)」を出して、どの分岐が動いているか確定する
import inspect
import logging
import logging as _logging
import logging.handlers
import os
import time
from collections import deque
from datetime import datetime, timezone  # ← 追加
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo
from datetime import timedelta

import anyio
from hl_core.config import load_settings
from hl_core.utils.logger import create_csv_formatter, setup_logger
# --- timezone resolver (JST fallback when tzdata is unavailable)
def _resolve_tz(name: str):
    """tzinfo を返却。ZoneInfo が使えない/見つからない場合はフォールバック。

    優先順位:
      1) zoneinfo.ZoneInfo(name) が使えるならそれを使う
      2) name が Asia/Tokyo/JST の場合は固定 +09:00 (DST 無し) にフォールバック
      3) それ以外は UTC
    """
    try:
        return ZoneInfo(name)
    except Exception:
        pass
    if name in ("Asia/Tokyo", "JST", "Japan"):
        return timezone(timedelta(hours=9))
    return timezone.utc
# 既存 import 群の最後あたりに追加
# Prefer the official SDK when running normally; fall back to a local stub
# during tests or when the SDK is unavailable.
try:  # pragma: no cover - import resolution path
    import os as _os
    if _os.getenv("PYTEST_CURRENT_TEST"):
        raise ImportError("force stub during tests")
    from hyperliquid.exchange import Exchange  # type: ignore
except Exception:  # pragma: no cover - fallback for tests/offline
    from hyperliquid_stub.exchange import Exchange  # type: ignore

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


logger = logging.getLogger(__name__)

# 役割: 「いま動いているプロセスが、どの strategy.py を読み込んでいるか」をログで確定させるための識別子
PFPL_STRATEGY_BUILD_ID = "ratio_probe_2025-12-26_01"


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

# 役割: モジュールが読み込まれた瞬間に一度だけINFOログを出し、"新しい strategy.py が実行された"ことを確実に可視化する
if not globals().get("_PFPL_STRATEGY_MODULE_BOOT_LOGGED"):
    logger.info(f"boot: PFPLStrategy build_id={PFPL_STRATEGY_BUILD_ID} file={__file__}")
    globals()["_PFPL_STRATEGY_MODULE_BOOT_LOGGED"] = True


class PFPLStrategy:
    """Price-Fair-Price-Lag bot"""

    # 役割: クールダウンと1秒あたりの最大発注数を守る（簡易レートリミット）
    def _can_fire(self, now_ts: float) -> bool:
        _logger = getattr(self, "log", None) or getattr(self, "logger", None)
        if not hasattr(self, "_last_order_ts"):
            self._last_order_ts = 0.0
        if not hasattr(self, "_order_count_window_start"):
            self._order_count_window_start = now_ts
            self._order_count_in_window = 0

        # クールダウン判定
        cd = float(getattr(self, "cooldown_sec", 0.0) or 0.0)
        if (now_ts - self._last_order_ts) < cd:
            if _logger:
                _logger.debug(
                    f"skip: cooldown {(now_ts - self._last_order_ts):.2f}s < {cd:.2f}s"
                )
            return False

        # 1秒窓の発注回数判定
        if (now_ts - self._order_count_window_start) >= 1.0:
            self._order_count_window_start = now_ts
            self._order_count_in_window = 0
        if self._order_count_in_window >= int(
            getattr(self, "max_order_per_sec", 1) or 1
        ):
            if _logger:
                _logger.debug("skip: rate_limit max_order_per_sec reached")
            return False

        return True

    # 役割: 取引所の minSizeUsd が分かればそれ、無ければ設定値 min_usd を返す
    def _effective_min_usd(self) -> float:
        exch_min = getattr(self, "minSizeUsd", None)
        if exch_min is None:
            meta = getattr(self, "market_meta", None) or getattr(self, "exchange_meta", None)
            if isinstance(meta, dict):
                exch_min = meta.get("minSizeUsd")
        fallback = float(getattr(self, "min_usd", 0.0) or 0.0)
        return float(exch_min) if exch_min is not None else fallback

    def _current_pos_usd(self, mid: Decimal | None = None) -> Decimal:
        """
        現在の建玉USDを返す。dry_run時は紙ポジション×midを使用。
        """
        if not getattr(self, "dry_run", False):
            try:
                return Decimal(str(self.pos_usd))
            except Exception:
                return Decimal("0")

        ref_mid = mid if mid is not None else getattr(self, "mid", None)
        if ref_mid is None:
            return Decimal("0")
        try:
            return Decimal(self.paper_pos) * Decimal(str(ref_mid))
        except Exception:
            return Decimal("0")

    def _projected_pos_usd(
        self,
        side: str,
        size: Decimal,
        *,
        mid: Decimal | None = None,
        price_hint: float | Decimal | None = None,
    ) -> Decimal:
        """
        約定後の建玉USDを試算し、max_position判定に使う。
        dry_run時は紙ポジションを基準に、実弾時はpos_usdを使う。
        """
        base = self._current_pos_usd(mid)
        ref_px: Decimal | None
        if mid is not None:
            ref_px = mid
        elif price_hint is not None:
            try:
                ref_px = Decimal(str(price_hint))
            except Exception:
                ref_px = None
        else:
            ref_px = None

        if ref_px is None:
            return base

        notional = size * ref_px
        if side.upper() == "BUY":
            return base + notional
        return base - notional

    # 役割: フィード辞書から対象銘柄の価格を取り出す。まず 'ETH-PERP' を探し、無ければ 'ETH'（self.feed_key）でフォールバックする
    def _get_from_feed(self, feed: dict[str, Any]) -> Any:
        if not feed:
            return None
        if self.target_symbol in feed:
            return feed[self.target_symbol]
        if self.feed_key in feed:
            return feed[self.feed_key]
        return None

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
        threshold: Decimal | float | str | None = None,
        threshold_pct: Decimal | float | str | None = None,
        spread_threshold: Decimal | float | str | None = None,
        pct_mode: str = "absolute",
        pct_threshold_value: float | None = None,
        spread_px: float | None = None,
        spread_threshold_px: float | None = None,
        abs_ok: bool | None = None,
        pct_ok: bool | None = None,
        spread_ok: bool | None = None,
    ) -> dict:
        _maybe_enable_test_propagation()
        logger = getattr(self, "logger", None) or getattr(self, "log", None) or logging.getLogger(__name__)
        now = time.time()

        diff_abs = mid_px - fair_px
        diff_pct = (diff_abs / fair_px) if fair_px else 0.0

        config = getattr(self, "config", {}) or {}

        def _resolve_threshold(
            *,
            key: str,
            attr_name: str,
            override: Decimal | float | str | None,
        ) -> Decimal:
            if override is not None:
                candidate = override
            elif hasattr(self, attr_name):
                candidate = getattr(self, attr_name)
            else:
                candidate = config.get(key)

            if isinstance(candidate, Decimal):
                return candidate
            if candidate is None:
                return Decimal("0")
            try:
                return Decimal(str(candidate))
            except Exception:
                return Decimal("0")

        thr_abs_dec = _resolve_threshold(
            key="threshold", attr_name="threshold", override=threshold
        )
        thr_pct_dec = _resolve_threshold(
            key="threshold_pct", attr_name="threshold_pct", override=threshold_pct
        )
        spread_thr_dec = _resolve_threshold(
            key="spread_threshold",
            attr_name="spread_threshold",
            override=spread_threshold,
        )

        thr_abs = float(thr_abs_dec)
        thr_pct = float(thr_pct_dec)
        spread_thr = float(spread_thr_dec)

        has_thr_abs = thr_abs_dec != Decimal("0")
        has_thr_pct = thr_pct_dec != Decimal("0")
        has_spread_thr = spread_thr_dec != Decimal("0")

        cooldown = getattr(self, "cooldown_sec", 0.0)
        max_pos = getattr(self, "max_position_usd", float("inf"))
        min_usd = getattr(self, "min_usd", 0.0)

        cooldown_ok = (now - (last_order_ts or 0.0)) >= cooldown
        abs_ok_val = (abs(diff_abs) >= thr_abs) if has_thr_abs else True
        pct_threshold_display = (
            pct_threshold_value if pct_threshold_value is not None else thr_pct
        )
        if pct_mode == "percentile":
            # abs(diff) ベースのパーセンタイル。サンプル不足時は許可。
            pct_ok_val = True if pct_threshold_value is None else abs(diff_abs) >= pct_threshold_value
        else:
            pct_ok_val = (abs(diff_pct) >= thr_pct) if has_thr_pct else True
        spread_thr_disp = spread_threshold_px if spread_threshold_px is not None else spread_thr
        if spread_threshold_px is not None and spread_px is not None:
            spread_ok_val = spread_px <= spread_threshold_px
        else:
            spread_ok_val = (abs(diff_abs) >= spread_thr) if has_spread_thr else True
        pos_ok = (abs(pos_usd) + order_usd) <= max_pos
        notional_ok = order_usd >= min_usd
        funding_ok = not funding_blocked

        # override が渡されていればそれを優先
        if abs_ok is not None:
            abs_ok_val = abs_ok
        if pct_ok is not None:
            pct_ok_val = pct_ok
        if spread_ok is not None:
            spread_ok_val = spread_ok

        # 方向の示唆（情報表示のみ）
        want_long = (diff_abs <= -thr_abs) if has_thr_abs else (diff_abs <= 0.0)
        if has_thr_pct:
            want_long = want_long or (diff_pct <= -thr_pct)

        want_short = (diff_abs >= thr_abs) if has_thr_abs else (diff_abs >= 0.0)
        if has_thr_pct:
            want_short = want_short or (diff_pct >= thr_pct)

        logger.debug(
            "decision mid=%.2f fair=%.2f d_abs=%+.4f d_pct=%+.5f | "
            "abs>=%.4f:%s pct(mode=%s)>=%.5f:%s spread(px)<=%.4f:%s | "
            "cooldown_ok=%s pos_ok=%s notional_ok=%s funding_ok=%s | "
            "long=%s short=%s",
            mid_px,
            fair_px,
            diff_abs,
            diff_pct,
            thr_abs,
            abs_ok_val,
            pct_mode,
            pct_threshold_display,
            pct_ok_val,
            spread_thr_disp,
            spread_ok_val,
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
            "abs_ok": abs_ok_val,
            "pct_ok": pct_ok_val,
            "spread_ok": spread_ok_val,
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

        # Dedicated per-symbol rotating file handler will be attached below after
        # config is loaded.
        # ── ① YAML と CLI のマージ ───────────────────────
        yml_path = Path(__file__).with_name("config.yaml")
        yaml_conf: dict[str, Any] = {}
        if yml_path.exists():
            with yml_path.open(encoding="utf-8") as f:
                raw_conf = f.read()
            yaml_conf = yaml.safe_load(raw_conf) or {}
        # 役割: config.yaml をデフォルトとして読み込み、run_bot.py 側の引数/外部設定で上書きできるようにする
        #       （testnet / dry_run / target_symbol / pair_cfg などの指定が効くように）
        self.config = {**yaml_conf, **config}
        # runtime hot-reload bookkeeping
        self._cfg_yml_path: Path | None = yml_path if yml_path.exists() else None
        self._cfg_mtime: float | None = (
            (self._cfg_yml_path.stat().st_mtime if self._cfg_yml_path else None)
        )
        self._last_cfg_check_ts: float = 0.0
        # Ensure per-symbol rotating log under logs/pfpl/<SYMBOL>.csv
        log_dir = Path("logs") / "pfpl"
        log_dir.mkdir(parents=True, exist_ok=True)
        symbol_log_path = (log_dir / f"{self.config.get('target_symbol', 'ETH-PERP')}.csv").resolve()
        existing_handler = next(
            (
                h
                for h in logger.handlers
                if isinstance(h, logging.handlers.TimedRotatingFileHandler)
                and getattr(h, "baseFilename", "") == str(symbol_log_path)
            ),
            None,
        )
        if existing_handler is None and str(symbol_log_path) not in PFPLStrategy._FILE_HANDLERS:
            fh = logging.handlers.TimedRotatingFileHandler(
                filename=str(symbol_log_path),
                when="midnight",
                interval=1,
                backupCount=14,
                encoding="utf-8",
                utc=False,
            )
            fh.setFormatter(create_csv_formatter(include_logger_name=False))
            logger.addHandler(fh)
            PFPLStrategy._FILE_HANDLERS.add(str(symbol_log_path))
        # Apply per-config log level if provided (e.g., DEBUG/INFO)
        lvl = str(self.config.get("log_level") or "").strip()
        if lvl:
            try:
                setup_logger(bot_name="pfpl", console_level=lvl, file_level=lvl)
            except Exception:
                pass
        funding_guard_cfg = self.config.get("funding_guard", {})
        if not isinstance(funding_guard_cfg, dict):
            funding_guard_cfg = {}

        self.funding_guard_enabled = _coerce_bool(
            funding_guard_cfg.get("enabled"), default=True
        )
        self.funding_guard_buffer_sec = int(
            funding_guard_cfg.get("buffer_sec", 300)
        )
        self.funding_guard_reenter_sec = int(
            funding_guard_cfg.get("reenter_sec", 120)
        )
        legacy_close_buffer = self.config.get("funding_close_buffer_secs", 120)
        self.funding_close_buffer_secs = int(

            funding_guard_cfg.get("buffer_sec", legacy_close_buffer)
        )
        # --- Order price offset percentage（デフォルト 0.0005 = 0.05 %）
        self.eps_pct = float(self.config.get("eps_pct", 0.0005))
        # --- taker_mode: True なら板に寄せて踏み行く価格を使う
        self.taker_mode = _coerce_bool(self.config.get("taker_mode"), default=False)

        # ── ② 通貨ペア・Semaphore 初期化 ─────────────────
        self.symbol = self.config.get("target_symbol", "ETH-PERP")
        sym_parts = self.symbol.split("-", 1)
        self.base_coin = sym_parts[0] if sym_parts else self.symbol
        self.target_symbol = self.symbol
        self.feed_key = self.base_coin

        self.log = logging.getLogger(__name__)

        max_ops = int(self.config.get("max_order_per_sec", 3))  # 1 秒あたり発注上限
        self.sem = semaphore or asyncio.Semaphore(max_ops)

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

        # Precedence: explicit config > environment > .env-derived settings
        account = _first_nonempty(
            self.config.get("account_address"),
            os.getenv("HL_ACCOUNT_ADDRESS"),
            os.getenv("HL_ACCOUNT_ADDR"),
            settings.account_address,
        )
        secret = _first_nonempty(
            self.config.get("private_key"),
            os.getenv("HL_PRIVATE_KEY"),
            os.getenv("HL_API_SECRET"),
            settings.private_key,
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
        # 型安全: eth_account が無い環境ではスタブ _Wallet を返すため、実行時のみ厳密
        wallet: Any = Account.from_key(self.secret)
        self.wallet = wallet
        base_url = (
            "https://api.hyperliquid-testnet.xyz"
            if self.config.get("testnet")
            else "https://api.hyperliquid.xyz"
        )
        self.exchange = Exchange(
            wallet,
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
        self.testnet = bool(self.config.get("testnet"))
        self.max_daily_orders = int(self.config.get("max_daily_orders", 500))
        # Timezone: default JST; allow override via config time_zone
        self.time_zone = str(self.config.get("time_zone", "Asia/Tokyo"))
        self._tz = _resolve_tz(self.time_zone)
        self._order_count = 0
        self._start_day = datetime.now(self._tz).date()
        self.enabled = True

        # 役割: クラス内で必ず使えるロガーを確保（self.log/self.logger が無い環境向けの保険）
        self.log = logging.getLogger(__name__)


        # 役割: 起動時に「build_id」と「このモジュールの実ファイルパス(__file__)」を必ず出して、読み込み元を確定する
        logger.info(f"boot: PFPLStrategy build_id={PFPL_STRATEGY_BUILD_ID} file={__file__}")
        # ── フィード保持用 -------------------------------------------------
        self.mid: Decimal | None = None  # 板 Mid (@1)
        self.idx: Decimal | None = None  # indexPrices
        self.ora: Decimal | None = None  # oraclePrices
        self.fair: Decimal | None = None  # 平均した公正価格
        self.allMids: dict[str, Any] = {}
        self.indexPrices: dict[str, Any] = {}
        self.oraclePrices: dict[str, Any] = {}
        self.best_bid: Decimal | None = None
        self.best_ask: Decimal | None = None
        # シグナル履歴（abs(diff)）でパーセンタイル判定に使う
        self._signal_hist: deque[tuple[float, float]] = deque()
        # 紙トレ用のポジション・PNL
        self.paper_pos = Decimal("0")
        self.paper_avg_px = Decimal("0")
        self.paper_realized = Decimal("0")
        self.paper_fee_bps_taker = Decimal(str(self.config.get("paper_fee_bps_taker", 0.05)))
        self.paper_fee_bps_maker = Decimal(str(self.config.get("paper_fee_bps_maker", 0.0)))
        self.paper_slip_bps = Decimal(str(self.config.get("paper_slip_bps", 0.5)))  # IOC許容スリップ（bps）

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
        # Per-symbol rotating handler already attached above

        logger.info("PFPLStrategy initialised with %s", self.config)
        try:
            logger.info(
                "daily reset schedule: tz=%s day=%s max_daily_orders=%d order_count=%d",
                getattr(self, "time_zone", "Asia/Tokyo"),
                self._start_day.isoformat(),
                self.max_daily_orders,
                self._order_count,
            )
        except Exception:
            pass

    # ---- runtime config reload (max_daily_orders immediate reflect) ----
    def _maybe_reload_runtime_config(self) -> None:
        try:
            now = time.time()
            if (now - getattr(self, "_last_cfg_check_ts", 0.0)) < 5.0:
                return
            self._last_cfg_check_ts = now
            path = getattr(self, "_cfg_yml_path", None)
            if not path or not path.exists():
                return
            mtime = path.stat().st_mtime
            prev = getattr(self, "_cfg_mtime", None)
            if prev is not None and mtime <= prev:
                return
            # reload yaml and merge
            with path.open(encoding="utf-8") as f:
                new_yaml = yaml.safe_load(f.read()) or {}
            old_limit = getattr(self, "max_daily_orders", None)
            self.config.update(new_yaml)
            self._cfg_mtime = mtime
            # reflect time_zone change immediately
            try:
                old_tzname = getattr(self, "time_zone", "Asia/Tokyo")
                new_tzname = str(self.config.get("time_zone", old_tzname))
                if new_tzname != old_tzname:
                    self.time_zone = new_tzname
                    self._tz = _resolve_tz(new_tzname)
                    logger.info(
                        "config reload: time_zone %s -> %s", old_tzname, new_tzname
                    )
            except Exception:
                pass
            # reflect max_daily_orders immediately
            try:
                new_limit = int(self.config.get("max_daily_orders", old_limit or 500))
            except Exception:
                new_limit = old_limit or 500
            if new_limit != old_limit:
                logger.info(
                    "config reload: max_daily_orders %s -> %s", old_limit, new_limit
                )
                self.max_daily_orders = new_limit
                try:
                    if getattr(self, "_order_count", 0) >= int(new_limit):
                        logger.warning(
                            "order_count %d >= new limit %d: trading disabled until daily reset",
                            getattr(self, "_order_count", 0),
                            int(new_limit),
                        )
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("config reload skipped: %s", exc)

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
        # Hot-reload config (throttled) so max_daily_orders reflects immediately
        self._maybe_reload_runtime_config()
        # Log and reset daily counters explicitly on UTC day change
        self._maybe_daily_reset_and_log()
        ch = msg.get("channel")
        feed = self.fair_feed
        combined_feed = feed not in {"indexPrices", "oraclePrices"}
        uses_index = feed == "indexPrices" or combined_feed
        uses_oracle = feed == "oraclePrices" or combined_feed

        should_eval = False
        fair_inputs_changed = False

        if ch == "allMids":  # 板 mid 群
            mids = (msg.get("data") or {}).get("mids") or {}
            self.allMids = mids
            mid_raw = self._get_from_feed(self.allMids)
            mid_key: str | None = None
            if self.target_symbol in self.allMids:
                mid_key = self.target_symbol
            elif self.feed_key in self.allMids:
                mid_key = self.feed_key
            if mid_raw is None:
                logger.debug(
                    "allMids: waiting for mid for %s (base=%s)",
                    self.target_symbol,
                    self.feed_key,
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
            self.indexPrices = prices
            price_val = self._get_from_feed(self.indexPrices)
            new_idx = Decimal(str(price_val)) if price_val is not None else None
            if new_idx != self.idx:
                self.idx = new_idx
                fair_inputs_changed = True
                if uses_index:
                    should_eval = True
        elif ch == "oraclePrices":  # オラクル価格
            prices = (msg.get("data") or {}).get("prices") or {}
            self.oraclePrices = prices
            price_val = self._get_from_feed(self.oraclePrices)
            new_ora = Decimal(str(price_val)) if price_val is not None else None
            if new_ora != self.ora:
                self.ora = new_ora
                fair_inputs_changed = True
                if uses_oracle:
                    should_eval = True
        elif ch == "activeAssetCtx":
            data = msg.get("data") or {}
            coin = str(data.get("coin") or self.base_coin).upper()
            if coin != str(self.base_coin).upper():
                return
            ctx = data.get("ctx") or {}
            # impactPxs があれば bid/ask を保持（紙トレ判定用）
            try:
                imp = ctx.get("impactPxs")
                if isinstance(imp, (list, tuple)) and len(imp) >= 2:
                    bid_raw = imp[0]
                    ask_raw = imp[1]
                    self.best_bid = Decimal(str(bid_raw)) if bid_raw is not None else None
                    self.best_ask = Decimal(str(ask_raw)) if ask_raw is not None else None
            except Exception:
                pass
            idx_raw = ctx.get("midPx") or ctx.get("markPx")
            ora_raw = ctx.get("oraclePx")
            updated = False
            if idx_raw is not None:
                try:
                    idx_val_dec = Decimal(str(idx_raw))
                except Exception:
                    idx_val_dec = None
                if idx_val_dec is not None:
                    self.indexPrices[self.symbol] = str(idx_val_dec)
                    self.indexPrices[self.base_coin] = str(idx_val_dec)
                    if self.idx != idx_val_dec:
                        self.idx = idx_val_dec
                        fair_inputs_changed = True
                        updated = True
                        if uses_index:
                            should_eval = True
            if ora_raw is not None:
                try:
                    ora_val_dec = Decimal(str(ora_raw))
                except Exception:
                    ora_val_dec = None
                if ora_val_dec is not None:
                    self.oraclePrices[self.symbol] = str(ora_val_dec)
                    self.oraclePrices[self.base_coin] = str(ora_val_dec)
                    if self.ora != ora_val_dec:
                        self.ora = ora_val_dec
                        fair_inputs_changed = True
                        updated = True
                        if uses_oracle:
                            should_eval = True
            if not updated:
                return
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
        if self.fair_feed not in {"indexPrices", "oraclePrices"}:
            idx_val = self._get_from_feed(self.indexPrices)
            ora_val = self._get_from_feed(self.oraclePrices)
            if idx_val is not None and ora_val is not None:
                try:
                    self.fair = (
                        Decimal(str(idx_val)) + Decimal(str(ora_val))
                    ) / Decimal("2")
                except Exception:
                    self.fair = None
            else:
                self.fair = None
            return

        primary_src_name = (
            "oraclePrices" if self.fair_feed == "oraclePrices" else "indexPrices"
        )
        primary_src = (
            self.oraclePrices if self.fair_feed == "oraclePrices" else self.indexPrices
        )
        fair_val = self._get_from_feed(primary_src)
        if fair_val is None:
            alt_src = (
                self.indexPrices if primary_src_name == "oraclePrices" else self.oraclePrices
            )
            fair_val = self._get_from_feed(alt_src)

        mid = getattr(self, "mid", None)
        # 役割: mid/fair のデバッグと欠損スキップ（prices:/skip:/edge(abs): を必ず出す）
        _logger = getattr(self, 'log', None) or getattr(self, 'logger', None)
        if _logger:
            _logger.debug(f"prices: mid={mid}, fair={fair_val}, mode={getattr(self,'mode',None)}, threshold={getattr(self,'threshold',None)}")
        if mid is None or fair_val is None:
            if _logger:
                _logger.debug(f"skip: missing price mid={mid} fair={fair_val}")
            self.fair = None
            return
        try:
            mid_decimal: Decimal = Decimal(str(mid))
            fair_decimal: Decimal = Decimal(str(fair_val))
            diff = mid_decimal - fair_decimal
        except Exception:
            if _logger:
                _logger.debug(f"skip: missing price mid={mid} fair={fair_val}")
            self.fair = None
            return
        if _logger:
            _logger.debug(f"edge(abs): {abs(diff)} (edge={diff})")
        # 役割: 後続処理でも参照できるよう、公正価格をプロパティへ反映

        self.fair = fair_decimal

    # ---------------------------------------------------------------- evaluate

    # src/bots/pfpl/strategy.py
    # ------------------------------------------------------------------ Tick loop
    # ─────────────────────────────────────────────────────────────
    def evaluate(self) -> None:
        import logging

        _logger = getattr(self, "logger", logging.getLogger(__name__))
        # Hot-reload config just before decision logic to reflect changes fast
        self._maybe_reload_runtime_config()
        try:

            _config = getattr(self, "config", {}) or {}

            def _fallback(key: str, attr_name: str | None = None) -> Any:
                if attr_name and hasattr(self, attr_name):
                    return getattr(self, attr_name)
                return _config.get(key)

            def _to_float(val: Any) -> float:
                if val is None:
                    return 0.0
                try:
                    return float(val)
                except Exception:
                    return 0.0

            def _fmt_optional(val: Any) -> Any:
                if val is None:
                    return None
                try:
                    return f"{float(val):.6f}"
                except Exception:
                    try:
                        return f"{val:.6f}"  # type: ignore[str-format]
                    except Exception:
                        return repr(val)

            _thr = _fallback("threshold", "threshold")
            _pct = _fallback("threshold_pct", "threshold_pct")
            _spr = _fallback("spread_threshold", "spread_threshold")

            _mid = locals().get("mid", locals().get("mid_px", getattr(self, "mid", None)))
            _fair = locals().get("fair", locals().get("fair_px", getattr(self, "fair", None)))
            if _mid is None or _fair is None:
                _diff = None
            else:
                try:
                    _diff = Decimal(str(_mid)) - Decimal(str(_fair))
                except Exception:
                    _diff = None
            _logger.debug(
                "DECISION_SNAPSHOT mid=%s fair=%s diff=%s | thr=%.6f spr=%.6f pct=%.6f",

                _fmt_optional(_mid),
                _fmt_optional(_fair),
                _fmt_optional(_diff),
                _to_float(_thr),
                _to_float(_spr),
                _to_float(_pct),

            )
        except Exception as _e:
            _logger.debug("DECISION_SNAPSHOT_UNAVAILABLE reason=%r", _e)

        _maybe_enable_test_propagation()
        if not self._check_funding_window():
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

        # ② 必要データ取得
        mid = self.mid
        fair = self.fair
        if mid is None or fair is None:
            return

        # ③ 発注可否（レート/最小発注額など）
        now_ts = time.time()
        can_fire = self._can_fire(now_ts)

        # ここで notion（USD）を見積もって最小発注額を満たすか確認する
        order_usd = float(getattr(self, "order_usd", 0.0) or 0.0)
        qty_tick = float(
            getattr(self, "qtyTick", 0.0)
            or getattr(self, "qty_tick", 0.0)
            or 0.0
        )
        mid_float = float(mid) if mid else 0.0
        qty_raw = order_usd / mid_float if mid_float else 0.0
        qty = (int(qty_raw / qty_tick) * qty_tick) if qty_tick > 0 else qty_raw
        notional = float(qty) * mid_float if mid_float else 0.0
        min_needed = self._effective_min_usd()

        _logger = getattr(self, "log", None) or getattr(self, "logger", None)
        notional_ok = notional >= min_needed
        if not notional_ok:
            if _logger:
                _logger.debug(
                    f"skip: notional {notional:.2f} < min_usd {min_needed:.2f} (qty={qty})"
                )

        diff = fair - mid  # USD 差（符号付き）
        diff_pct = diff / mid * Decimal("100")  # 乖離率 %（符号付き）
        abs_diff = abs(diff)
        pct_diff = abs(diff_pct)

        # ④ 閾値判定
        th_abs = Decimal(str(self.config.get("threshold", "1.0")))  # USD
        pct_mode = str(self.config.get("threshold_pct_mode", "absolute")).lower()
        pct_quantile = float(
            self.config.get(
                "threshold_pct_quantile",
                self.config.get("threshold_pct", 0.0),
            )
        )
        pct_window = float(self.config.get("threshold_pct_window_sec", 900.0))
        pct_min_samples = int(self.config.get("threshold_pct_min_samples", 50))
        th_pct = Decimal(str(self.config.get("threshold_pct", "0.05")))  # %
        spread_thr_usd = float(self.config.get("spread_threshold", 0.0))
        spread_thr_bps = float(self.config.get("spread_threshold_bps", 0.0))
        spread_thr_px = spread_thr_usd
        if spread_thr_px <= 0 and spread_thr_bps > 0 and mid:
            spread_thr_px = float(mid) * (spread_thr_bps / 10000)
        bid = getattr(self, "best_bid", None)
        ask = getattr(self, "best_ask", None)
        spread_px = float(ask - bid) if (bid is not None and ask is not None) else None
        spread_ok = True if spread_thr_px <= 0 or spread_px is None else spread_px <= spread_thr_px

        pct_threshold_value: float | None = None
        if pct_mode == "percentile":
            pct_threshold_value = self._update_signal_hist(
                abs_diff=float(abs_diff),
                now_ts=now,
                window_sec=pct_window,
                quantile=pct_quantile,
                min_samples=pct_min_samples,
            )
            pct_ok = True if pct_threshold_value is None else float(abs_diff) >= pct_threshold_value
        else:
            pct_threshold_value = float(th_pct)
            pct_ok = pct_diff >= th_pct

        abs_ok = abs_diff >= th_abs

        # spreadフィルタに掛かったら早期リターン
        if not spread_ok:
            self._debug_evaluate_signal(
                mid_px=float(mid),
                fair_px=float(fair),
                order_usd=float(self.order_usd),
                pos_usd=float(self.pos_usd),
                last_order_ts=self.last_ts or None,
                funding_blocked=self._funding_pause,
                threshold=th_abs,
                threshold_pct=pct_threshold_value,
                spread_threshold=Decimal(str(spread_thr_px)),
                pct_mode=pct_mode,
                pct_threshold_value=pct_threshold_value,
                spread_px=spread_px,
                spread_threshold_px=spread_thr_px,
                abs_ok=abs_ok,
                pct_ok=pct_ok,
                spread_ok=spread_ok,
            )
            return

        self._debug_evaluate_signal(
            mid_px=float(mid),
            fair_px=float(fair),
            order_usd=float(self.order_usd),
            pos_usd=float(self.pos_usd),
            last_order_ts=self.last_ts or None,
            funding_blocked=self._funding_pause,
            threshold=th_abs,
            threshold_pct=pct_threshold_value,
            spread_threshold=Decimal(str(spread_thr_px)),
            pct_mode=pct_mode,
            pct_threshold_value=pct_threshold_value,
            spread_px=spread_px,
            spread_threshold_px=spread_thr_px,
            abs_ok=abs_ok,
            pct_ok=pct_ok,
            spread_ok=spread_ok,
        )
        if not can_fire:
            return
        if not notional_ok:
            return
        mode = self.config.get("mode", "both")  # both / either

        if mode == "abs":
            if not abs_ok:
                return
        elif mode == "pct":
            if not pct_ok:
                return
        elif mode == "either":
            if not (abs_ok or pct_ok):
                return
        else:  # default = both
            if not abs_ok or not pct_ok:
                return

        # ⑤ 発注サイド決定
        side = "BUY" if fair > mid else "SELL"

        # ⑥ 連続同方向防止
        if side == self.last_side and now - self.last_ts < self.cooldown:
            return

        # ⑦ 発注サイズ計算
        is_buy = side == "BUY"
        mid_dec = mid
        if mid_dec <= 0:
            return

        # 発注に使う想定 limit_px（place_order と同じヘルパーで近似）
        limit_px_est = self._taker_limit_price(
            side,
            self._price_with_offset(float(mid_dec), side),
            mid_dec,
        )
        limit_px_dec = (
            Decimal(str(limit_px_est))
            if limit_px_est is not None and limit_px_est > 0
            else mid_dec
        )
        if limit_px_dec <= 0:
            limit_px_dec = mid_dec

        raw_size = (self.order_usd / limit_px_dec) if limit_px_dec > 0 else Decimal("0")
        try:
            size = raw_size.quantize(self.qty_tick, rounding=ROUND_DOWN)
        except InvalidOperation:
            logger.error(
                "quantize failed for raw size %s with qty_tick %s",
                raw_size,
                self.qty_tick,
            )
            return
        # 役割: 丸め前後やポジション上限による切り詰めを追えるように保持
        size_pre_limit = size
        pos_limit_applied = False
        pos_limit_remaining_base: Decimal | None = None
        pos_limit_cur_abs_pos: Decimal | None = None
        pos_limit_max_abs_pos: Decimal | None = None
        pos_limit_proj_abs_pos: Decimal | None = None
        if size <= 0:
            logger.debug(
                "size %.6f quantized to zero with tick %s → skip",
                raw_size,
                self.qty_tick,
            )
            return
        if (size * mid_dec) < self.min_usd:
            # 役割: min_usd skip の原因特定用に、丸め前/丸め後のサイズと USD を同時に記録する
            order_usd = self.order_usd
            limit_px = limit_px_dec
            raw_size_dbg = (
                (order_usd / limit_px) if (limit_px is not None and limit_px > 0) else None
            )
            raw_usd_dbg = (
                (raw_size_dbg * mid_dec) if (raw_size_dbg is not None and mid_dec is not None) else None
            )
            rounded_usd = size * mid_dec if mid_dec is not None else None
            mid = mid_dec
            raw_usd = raw_usd_dbg
            usd = rounded_usd
            min_usd = self.min_usd
            logger.debug(
                "SIZING_SNAPSHOT build_id=%s loc=%s:%s order_usd=%s limit_px=%s mid=%s raw_size=%s raw_usd=%s rounded_size=%s rounded_usd=%s min_usd=%s",
                PFPL_STRATEGY_BUILD_ID,
                __file__,
                inspect.currentframe().f_lineno,
                order_usd,
                limit_px,
                mid,
                raw_size,
                raw_usd,
                size,
                usd,
                min_usd,
            )
            # 役割: raw_usd(=本来10USD)が、どの上限/係数で rounded_usd(=2.08USD)まで落ちたかを「候補変数ごと」に特定する
            cap_ratio = (
                (usd / raw_usd)
                if (raw_usd is not None and raw_usd > 0 and usd is not None)
                else None
            )
            logger.debug(
                "SIZING_LIMITERS build_id=%s loc=%s:%s cap_ratio=%s qty_tick=%s max_order_usd=%s max_trade_usd=%s max_order_qty=%s max_trade_qty=%s size_scale=%s risk_scale=%s remaining_usd=%s remaining_qty=%s",
                PFPL_STRATEGY_BUILD_ID,
                __file__,
                inspect.currentframe().f_lineno,
                cap_ratio,
                locals().get("qty_tick") or locals().get("qty_step") or locals().get("sz_tick"),
                locals().get("max_order_usd") or locals().get("order_cap_usd") or locals().get("cap_usd"),
                locals().get("max_trade_usd") or locals().get("trade_cap_usd"),
                locals().get("max_order_qty") or locals().get("order_cap_qty") or locals().get("cap_qty"),
                locals().get("max_trade_qty") or locals().get("trade_cap_qty"),
                locals().get("size_scale") or locals().get("scale"),
                locals().get("risk_scale"),
                locals().get("remaining_usd") or locals().get("pos_remaining_usd"),
                locals().get("remaining_qty") or locals().get("pos_remaining_qty"),
            )
            # 役割: cap_ratio で潰された「実効USD(target_usd)」が、どのローカル変数（edge/diff等）と一致するかを特定する
            _target_usd = (
                float(raw_usd) * float(cap_ratio)
                if (cap_ratio is not None and raw_usd is not None)
                else None
            )
            if _target_usd is not None:
                _cands = []
                for _k, _v in locals().items():
                    if _k.startswith("_"):
                        continue
                    if _k in (
                        "usd",
                        "raw_usd",
                        "order_usd",
                        "cap_ratio",
                        "size",
                        "raw_size",
                        "limit_px",
                        "mid",
                    ):
                        continue
                    if isinstance(_v, bool):
                        continue
                    try:
                        _vf = float(_v)
                    except Exception:
                        continue
                    # 近さ判定（target_usd の ±5% または ±0.05USD）
                    if abs(_vf - _target_usd) <= max(0.05, abs(_target_usd) * 0.05):
                        _cands.append((abs(_vf - _target_usd), _k, _vf))

                _cands.sort(key=lambda x: x[0])
                _top = ", ".join([f"{k}={v} (Δ={d:.6g})" for d, k, v in _cands[:10]])

                logger.debug(
                    "SIZING_TARGET_MATCH build_id=%s loc=%s:%s target_usd=%s top=%s",
                    PFPL_STRATEGY_BUILD_ID,
                    __file__,
                    inspect.currentframe().f_lineno,
                    _target_usd,
                    _top,
                )
            # 役割: cap_ratio で潰された「実効USD(target_usd)」が、どのローカル変数（edge/diff等）と一致するかを特定する
            _target_usd = (
                float(raw_usd) * float(cap_ratio)
                if (cap_ratio is not None and raw_usd is not None)
                else None
            )
            if _target_usd is not None:
                _cands = []
                for _k, _v in locals().items():
                    if _k.startswith("_"):
                        continue
                    if _k in (
                        "usd",
                        "raw_usd",
                        "order_usd",
                        "cap_ratio",
                        "size",
                        "raw_size",
                        "limit_px",
                        "mid",
                    ):
                        continue
                    if isinstance(_v, bool):
                        continue
                    try:
                        _vf = float(_v)
                    except Exception:
                        continue
                    # 近さ判定（target_usd の ±5% または ±0.05USD）
                    if abs(_vf - _target_usd) <= max(0.05, abs(_target_usd) * 0.05):
                        _cands.append((abs(_vf - _target_usd), _k, _vf))

                _cands.sort(key=lambda x: x[0])
                _top = ", ".join([f"{k}={v} (Δ={d:.6g})" for d, k, v in _cands[:10]])

                logger.debug(
                    "SIZING_TARGET_MATCH build_id=%s loc=%s:%s target_usd=%s top=%s",
                    PFPL_STRATEGY_BUILD_ID,
                    __file__,
                    inspect.currentframe().f_lineno,
                    _target_usd,
                    _top,
                )
            # 役割: cap_ratio(=usd/raw_usd) と一致/近い「変数そのもの」や「比(A/B)」を自動で炙り出してログに出す
            cap_ratio_f = float(cap_ratio) if cap_ratio is not None else None
            if cap_ratio_f is not None:
                _skip_names = {
                    "cap_ratio", "cap_ratio_f",
                    "raw_usd", "raw_size",
                    "order_usd", "usd", "size",
                    "limit_px", "mid", "min_usd",
                }

                _vals = []  # (name, float_value)

                def _add_numeric(_name, _v):
                    # 役割: 数値として扱えるものだけ集める（bool/文字列/巨大値は除外）
                    if _name in _skip_names:
                        return
                    if isinstance(_v, bool):
                        return
                    try:
                        _vf = float(_v)
                    except Exception:
                        return
                    if not (abs(_vf) > 0.0 and abs(_vf) <= 1.0e9):
                        return
                    _vals.append((_name, _vf))

                # locals() から拾う
                for _k, _v in locals().items():
                    _add_numeric(f"local.{_k}", _v)

                # self / config から拾う（属性に倍率が隠れているケースが多い）
                _self = locals().get("self")
                if _self is not None:
                    for _k, _v in getattr(_self, "__dict__", {}).items():
                        _add_numeric(f"self.{_k}", _v)

                    for _attr in ("cfg", "config", "params", "settings"):
                        _obj = getattr(_self, _attr, None)
                        if _obj is None:
                            continue
                        if isinstance(_obj, dict):
                            for _k, _v in _obj.items():
                                _add_numeric(f"self.{_attr}.{_k}", _v)
                        else:
                            for _k, _v in getattr(_obj, "__dict__", {}).items():
                                _add_numeric(f"self.{_attr}.{_k}", _v)

                # 1) 「変数そのもの」が cap_ratio に近いか（直接マッチ）
                _direct = []
                for _name, _vf in _vals:
                    if 0.0 < abs(_vf) <= 1.0:
                        _direct.append((abs(_vf - cap_ratio_f), _name, _vf))
                _direct.sort(key=lambda x: x[0])
                _top_direct = ", ".join([f"{n}={v} (Δ={d:.6g})" for d, n, v in _direct[:8]])
                logger.debug("SIZING_MATCH cap_ratio=%s top=%s", cap_ratio, _top_direct)

                # 2) 「比(A/B)」が cap_ratio に近いか（間接マッチ） → これで正体を特定する
                #    ※ cap_ratio が (edge_abs / edge_cap) などで作られている場合、ここで引っかかる
                _ratio_hits = []
                _vals_limited = _vals[:80]  # 役割: 計算量を抑える（多すぎると重い）
                for i in range(len(_vals_limited)):
                    a_name, a_val = _vals_limited[i]
                    for j in range(len(_vals_limited)):
                        if i == j:
                            continue
                        b_name, b_val = _vals_limited[j]
                        if b_val == 0:
                            continue
                        r = abs(a_val / b_val)
                        d = abs(r - cap_ratio_f)
                        if d <= 0.02:
                            _ratio_hits.append((d, a_name, a_val, b_name, b_val, r))

                _ratio_hits.sort(key=lambda x: x[0])
                _top_ratio = ", ".join(
                    [f"{an}/{bn}={rv:.6g} (Δ={d:.6g})" for d, an, av, bn, bv, rv in _ratio_hits[:8]]
                )
                logger.debug("SIZING_RATIO_MATCH cap_ratio=%s top=%s", cap_ratio, _top_ratio)
            logger.debug(
                "size %.4f USD %.2f < min_usd %.2f → skip",
                size,
                size * mid_dec,
                self.min_usd,
            )
            return

        # ⑧ 建玉超過チェック（方向込み + 自動切り詰め）
        # 役割:
        # - ポジを「減らす」方向の注文は、上限付近でも通す（exit/縮小が詰まらない）
        # - ポジを「増やす」方向で上限を超える場合は、入る分だけサイズを切り詰める（orders=0 を回避）
        # 現在ポジ（base）: dry_run は paper_pos、実弾は pos_usd/mid 近似
        pos_usd_signed = self._current_pos_usd(mid_dec)
        paper_pos = (pos_usd_signed / mid_dec) if mid_dec > 0 else Decimal("0")

        cur_abs_pos = abs(paper_pos)

        pos_limit_usd = self.max_pos
        max_abs_pos = (pos_limit_usd / mid_dec) if mid_dec > 0 else Decimal("0")

        proj_pos = paper_pos + (size if is_buy else -size)
        proj_abs_pos = abs(proj_pos)
        proj_notional = proj_abs_pos * mid_dec
        pos_limit_cur_abs_pos = cur_abs_pos
        pos_limit_max_abs_pos = max_abs_pos
        pos_limit_proj_abs_pos = proj_abs_pos

        # 「上限超過」かつ「今回の注文で abs(pos) が増える」時だけ制限（減らす注文は止めない）
        if (proj_abs_pos > max_abs_pos) and (proj_abs_pos > cur_abs_pos):
            remaining_base = max_abs_pos - cur_abs_pos

            # もう 1 目盛りも増やせないならスキップ
            if remaining_base <= 0:
                self._log_pos_limit_skip(kind="proj", value=float(proj_notional))
                return

            # 入る分だけ切り詰める（切り詰め後のサイズで projected を更新）
            size_before_cap = size
            size = min(size, remaining_base)
            try:
                size = size.quantize(self.qty_tick, rounding=ROUND_DOWN)
            except InvalidOperation:
                return
            if size <= 0:
                self._log_pos_limit_skip(kind="proj", value=float(proj_notional))
                return
            if size != size_before_cap:
                pos_limit_applied = True
                pos_limit_remaining_base = remaining_base

            proj_pos = paper_pos + (size if is_buy else -size)
            proj_notional = abs(proj_pos) * mid_dec

        # 切り詰め後に min_usd を割るならスキップ（サイズが小さすぎる）
        if (size * mid_dec) < self.min_usd:
            # 役割: min_usd skip の原因特定用に、丸め前/丸め後のサイズと USD を同時に記録する
            order_usd = self.order_usd
            limit_px = limit_px_dec
            raw_size_dbg = (
                (order_usd / limit_px) if (limit_px is not None and limit_px > 0) else None
            )
            raw_usd_dbg = (
                (raw_size_dbg * mid_dec) if (raw_size_dbg is not None and mid_dec is not None) else None
            )
            rounded_usd = size * mid_dec if mid_dec is not None else None
            mid = mid_dec
            raw_usd = raw_usd_dbg
            usd = rounded_usd
            min_usd = self.min_usd
            logger.debug(
                "SIZING_SNAPSHOT build_id=%s loc=%s:%s order_usd=%s limit_px=%s mid=%s raw_size=%s raw_usd=%s rounded_size=%s rounded_usd=%s min_usd=%s",
                PFPL_STRATEGY_BUILD_ID,
                __file__,
                inspect.currentframe().f_lineno,
                order_usd,
                limit_px,
                mid,
                raw_size,
                raw_usd,
                size,
                usd,
                min_usd,
            )
            # 役割: raw_usd(=本来10USD)が、どの上限/係数で rounded_usd(=2.08USD)まで落ちたかを「候補変数ごと」に特定する
            cap_ratio = (
                (usd / raw_usd)
                if (raw_usd is not None and raw_usd > 0 and usd is not None)
                else None
            )
            logger.debug(
                "SIZING_LIMITERS build_id=%s loc=%s:%s cap_ratio=%s qty_tick=%s max_order_usd=%s max_trade_usd=%s max_order_qty=%s max_trade_qty=%s size_scale=%s risk_scale=%s remaining_usd=%s remaining_qty=%s",
                PFPL_STRATEGY_BUILD_ID,
                __file__,
                inspect.currentframe().f_lineno,
                cap_ratio,
                locals().get("qty_tick") or locals().get("qty_step") or locals().get("sz_tick"),
                locals().get("max_order_usd") or locals().get("order_cap_usd") or locals().get("cap_usd"),
                locals().get("max_trade_usd") or locals().get("trade_cap_usd"),
                locals().get("max_order_qty") or locals().get("order_cap_qty") or locals().get("cap_qty"),
                locals().get("max_trade_qty") or locals().get("trade_cap_qty"),
                locals().get("size_scale") or locals().get("scale"),
                locals().get("risk_scale"),
                locals().get("remaining_usd") or locals().get("pos_remaining_usd"),
                locals().get("remaining_qty") or locals().get("pos_remaining_qty"),
            )
            # 役割: cap_ratio(=usd/raw_usd) と一致/近い「変数そのもの」や「比(A/B)」を自動で炙り出してログに出す
            cap_ratio_f = float(cap_ratio) if cap_ratio is not None else None
            if cap_ratio_f is not None:
                _skip_names = {
                    "cap_ratio", "cap_ratio_f",
                    "raw_usd", "raw_size",
                    "order_usd", "usd", "size",
                    "limit_px", "mid", "min_usd",
                }

                _vals = []  # (name, float_value)

                def _add_numeric(_name, _v):
                    # 役割: 数値として扱えるものだけ集める（bool/文字列/巨大値は除外）
                    if _name in _skip_names:
                        return
                    if isinstance(_v, bool):
                        return
                    try:
                        _vf = float(_v)
                    except Exception:
                        return
                    if not (abs(_vf) > 0.0 and abs(_vf) <= 1.0e9):
                        return
                    _vals.append((_name, _vf))

                # locals() から拾う
                for _k, _v in locals().items():
                    _add_numeric(f"local.{_k}", _v)

                # self / config から拾う（属性に倍率が隠れているケースが多い）
                _self = locals().get("self")
                if _self is not None:
                    for _k, _v in getattr(_self, "__dict__", {}).items():
                        _add_numeric(f"self.{_k}", _v)

                    for _attr in ("cfg", "config", "params", "settings"):
                        _obj = getattr(_self, _attr, None)
                        if _obj is None:
                            continue
                        if isinstance(_obj, dict):
                            for _k, _v in _obj.items():
                                _add_numeric(f"self.{_attr}.{_k}", _v)
                        else:
                            for _k, _v in getattr(_obj, "__dict__", {}).items():
                                _add_numeric(f"self.{_attr}.{_k}", _v)

                # 1) 「変数そのもの」が cap_ratio に近いか（直接マッチ）
                _direct = []
                for _name, _vf in _vals:
                    if 0.0 < abs(_vf) <= 1.0:
                        _direct.append((abs(_vf - cap_ratio_f), _name, _vf))
                _direct.sort(key=lambda x: x[0])
                _top_direct = ", ".join([f"{n}={v} (Δ={d:.6g})" for d, n, v in _direct[:8]])
                logger.debug("SIZING_MATCH cap_ratio=%s top=%s", cap_ratio, _top_direct)

                # 2) 「比(A/B)」が cap_ratio に近いか（間接マッチ） → これで正体を特定する
                #    ※ cap_ratio が (edge_abs / edge_cap) などで作られている場合、ここで引っかかる
                _ratio_hits = []
                _vals_limited = _vals[:80]  # 役割: 計算量を抑える（多すぎると重い）
                for i in range(len(_vals_limited)):
                    a_name, a_val = _vals_limited[i]
                    for j in range(len(_vals_limited)):
                        if i == j:
                            continue
                        b_name, b_val = _vals_limited[j]
                        if b_val == 0:
                            continue
                        r = abs(a_val / b_val)
                        d = abs(r - cap_ratio_f)
                        if d <= 0.02:
                            _ratio_hits.append((d, a_name, a_val, b_name, b_val, r))

                _ratio_hits.sort(key=lambda x: x[0])
                _top_ratio = ", ".join(
                    [f"{an}/{bn}={rv:.6g} (Δ={d:.6g})" for d, an, av, bn, bv, rv in _ratio_hits[:8]]
                )
                logger.debug("SIZING_RATIO_MATCH cap_ratio=%s top=%s", cap_ratio, _top_ratio)
            logger.debug(
                "size %.4f USD %.2f < min_usd %.2f → skip",
                float(size),
                float(size * mid_dec),
                float(self.min_usd),
            )
            return

        # 重要: 以降の注文サイズは size を使う（order_usd/limit_px を再計算しない）

        # ⑨ 発注
        # 役割: ここまで来たら「発注してOK」。カウンタだけ進め、既存の発注ロジックへ続行
        self._last_order_ts = now_ts
        self._order_count_in_window = (
            getattr(self, "_order_count_in_window", 0) or 0
        ) + 1
        asyncio.create_task(self.place_order(side, float(size)))

    # ---------------------------------------------------------------- order

    def _paper_fill(self, side: str, qty: Decimal, px: Decimal) -> Decimal:
        """Dry-runで簡易約定を適用し、実現損益を返す。"""
        if qty <= 0:
            return Decimal("0")

        side_sign = Decimal("1") if side.upper() == "BUY" else Decimal("-1")
        cur_pos = getattr(self, "paper_pos", Decimal("0"))
        cur_avg = getattr(self, "paper_avg_px", Decimal("0"))

        # 同方向の追加建て: 平均価格を更新してPNLなし
        if cur_pos == 0 or (cur_pos > 0 and side_sign > 0) or (cur_pos < 0 and side_sign < 0):
            new_pos = cur_pos + side_sign * qty
            try:
                new_avg = ((abs(cur_pos) * cur_avg) + (qty * px)) / abs(new_pos)
            except Exception:
                new_avg = px
            self.paper_pos = new_pos
            self.paper_avg_px = new_avg
            return Decimal("0")

        # 反対売買: 決済分をPNL計算、残りがあれば反転建て
        close_qty = min(abs(cur_pos), qty)
        if cur_pos > 0:
            realized = (px - cur_avg) * close_qty  # long決済
        else:
            realized = (cur_avg - px) * close_qty  # short決済

        remain_qty = qty - close_qty
        self.paper_realized += realized

        if close_qty == abs(cur_pos):
            # 全決済した場合、残りがあれば新規反対建て
            self.paper_pos = side_sign * remain_qty
            self.paper_avg_px = px if remain_qty > 0 else Decimal("0")
        else:
            # 部分決済（元ポジションが残る）
            self.paper_pos = cur_pos + side_sign * close_qty
            self.paper_avg_px = cur_avg

        return realized

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

        limit_px = self._taker_limit_price(side, limit_px, mid_value)

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

        # --- Dry-run: 紙で約定をシミュレートする --------------------------------
        test_mode = bool(os.getenv("PYTEST_CURRENT_TEST"))
        if self.dry_run and not test_mode:
            logger.info("[DRY-RUN] %s %.4f %s", side, size, self.symbol)
            logger.info("[DRY-RUN] payload=%s", order_kwargs)

            # 紙用の疑似約定（IOC想定）。bid/askが無いときは mid をフォールバック。
            qty_dec = Decimal(str(order_kwargs.get("sz", size_dec)))
            px_src = order_kwargs.get("limit_px")
            px_val = (
                Decimal(str(px_src))
                if px_src is not None
                else Decimal(str(mid_value or limit_px or 0))
            )

            bid = getattr(self, "best_bid", None)
            ask = getattr(self, "best_ask", None)
            can_fill = True
            miss_reason: str | None = None
            slip_pct = (self.paper_slip_bps or Decimal("0")) / Decimal("10000")
            if px_val is None:
                can_fill = False
                miss_reason = "paper price unavailable"
            elif bid is not None and ask is not None:
                if is_buy:
                    threshold = ask * (Decimal("1") - slip_pct)
                    if px_val < threshold:
                        can_fill = False
                        miss_reason = f"paper ioc miss ask={ask}"
                else:
                    threshold = bid * (Decimal("1") + slip_pct)
                    if px_val > threshold:
                        can_fill = False
                        miss_reason = f"paper ioc miss bid={bid}"

            if not can_fill:
                logger.info(
                    "ORDER_STATUS symbol=%s asset=%s oid=%s status=%s filled=0 remaining=%.4f err=%s raw=%s",
                    getattr(self, "symbol", None),
                    getattr(self, "base_coin", None),
                    "paper",
                    "paper",
                    float(qty_dec),
                    miss_reason,
                    None,
                )
                self.last_ts = time.time()
                self.last_side = side
                return

            realized = self._paper_fill(side, qty_dec, px_val)
            notional = (px_val or Decimal("0")) * qty_dec
            fee_rate = self.paper_fee_bps_taker if getattr(self, "taker_mode", False) else self.paper_fee_bps_maker
            fee = (notional * fee_rate) / Decimal("10000") if fee_rate else Decimal("0")
            if fee:
                realized -= fee
                self.paper_realized -= fee

            paper_resp = {
                "status": "paper",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {
                                "status": "filled",
                                "filled": float(qty_dec),
                                "remaining": 0.0,
                                "avgPx": float(px_val),
                            }
                        ]
                    },
                },
            }
            logger.info(
                "ORDER_RAW_RESPONSE symbol=%s resp=%s",
                getattr(self, "symbol", None),
                paper_resp,
            )
            logger.info(
                "ORDER_STATUS symbol=%s asset=%s oid=%s status=%s filled=%.4f remaining=0 err=%s raw=%s",
                getattr(self, "symbol", None),
                getattr(self, "base_coin", None),
                "paper",
                "paper",
                float(qty_dec),
                None,
                paper_resp,
            )
            if realized != 0:
                logger.info(
                    "PAPER_PNL symbol=%s realized=%.6f cum=%.6f pos=%.6f avg_px=%.4f",
                    getattr(self, "symbol", None),
                    float(realized),
                    float(self.paper_realized),
                    float(self.paper_pos),
                    float(self.paper_avg_px),
                )
            # pytest ではモックされた exchange.order をスレッド経由で呼び、非ブロッキング挙動を検証できるようにする
            try:
                if os.getenv("PYTEST_CURRENT_TEST"):
                    order_fn = getattr(self.exchange, "order", None)
                    if callable(order_fn):
                        await asyncio.to_thread(order_fn, **order_kwargs)
            except Exception:
                pass
                pass
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

    # ------------------------------------------------------------------ daily reset
    def _maybe_daily_reset_and_log(self) -> None:
        """UTC日付の変化を検知して、カウンタを明示ログ付きでリセットする"""
        tz = getattr(self, "_tz", timezone.utc)
        today = datetime.now(tz).date()
        if today != getattr(self, "_start_day", today):
            prev_day = getattr(self, "_start_day", today)
            prev_cnt = int(getattr(self, "_order_count", 0) or 0)
            self._start_day = today
            self._order_count = 0
            logger.info(
                "daily reset: tz=%s day %s -> %s | order_count %d -> 0 | max_daily_orders=%d",
                getattr(self, "time_zone", "Asia/Tokyo"),
                getattr(prev_day, "isoformat", lambda: str(prev_day))(),
                today.isoformat(),
                prev_cnt,
                int(getattr(self, "max_daily_orders", 0) or 0),
            )

    # ------------------------------------------------------------------ limits
    def _check_limits(self) -> bool:
        """日次の発注数制限を超えていないか確認（建玉制限は発注直前で方向込み判定）"""
        tz = getattr(self, "_tz", timezone.utc)
        today = datetime.now(tz).date()
        if today != self._start_day:  # 日付が変わったらリセット
            self._start_day = today
            self._order_count = 0

        if self._order_count >= self.max_daily_orders:
            logger.warning("daily order-limit reached → trading disabled")
            return False

        return True

    def _log_pos_limit_skip(self, *, kind: str, value: float) -> None:
        """
        pos_limit超過時のログを一定時間内で集計してまとめる。
        kind: "cur"（現建玉）/ "proj"（発注後の見込み）
        """
        now_ts = time.time()
        window = getattr(self, "_pos_limit_window_sec", 60)
        if not hasattr(self, "_pos_limit_hits_detail"):
            self._pos_limit_hits_detail = {"total": 0, "cur": 0, "proj": 0}
            self._pos_limit_last_log_ts = 0.0
            self._pos_limit_max_value = 0.0

        detail: dict[str, int] = self._pos_limit_hits_detail  # type: ignore[assignment]
        detail["total"] = detail.get("total", 0) + 1
        detail[kind] = detail.get(kind, 0) + 1
        self._pos_limit_max_value = max(
            getattr(self, "_pos_limit_max_value", 0.0), float(value)
        )

        if self._pos_limit_last_log_ts == 0 or now_ts - self._pos_limit_last_log_ts >= window:
            logger.info(
                "pos_limit skip: max=%.2f USD 直近%ds total=%d (cur=%d proj=%d) max_seen=%.2f",
                self.max_pos,
                window,
                detail.get("total", 0),
                detail.get("cur", 0),
                detail.get("proj", 0),
                getattr(self, "_pos_limit_max_value", 0.0),
            )
            self._pos_limit_last_log_ts = now_ts
            self._pos_limit_hits_detail = {"total": 0, "cur": 0, "proj": 0}
            self._pos_limit_max_value = 0.0

    def _update_signal_hist(
        self,
        *,
        abs_diff: float,
        now_ts: float,
        window_sec: float,
        quantile: float,
        min_samples: int = 50,
    ) -> float | None:
        """
        abs(diff) の履歴を window_sec で保持し、quantile(0-1)の値を返す。
        サンプル不足なら None を返し、フィルタは緩めに通す。
        """
        if not 0 <= quantile <= 1:
            return None
        if min_samples <= 0:
            min_samples = 1
        dq: deque[tuple[float, float]] = getattr(self, "_signal_hist", deque())
        dq.append((now_ts, float(abs_diff)))
        cutoff = now_ts - window_sec
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        # 保存し直す
        self._signal_hist = dq
        if len(dq) < min_samples:
            return None
        vals = sorted(v for _, v in dq)
        if not vals:
            return None
        k = (len(vals) - 1) * quantile
        lo = int(k)
        hi = min(lo + 1, len(vals) - 1)
        if lo == hi:
            return vals[lo]
        frac = k - lo
        return vals[lo] + (vals[hi] - vals[lo]) * frac

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

    def _taker_limit_price(
        self,
        side: str,
        candidate: float | None,
        mid_value: Decimal | None,
    ) -> float | None:
        """
        taker_mode の場合に板寄りの価格へ寄せて、IOC でもヒットしやすくする。

        - BUY: ask * (1 + cushion) で上乗せして踏みに行く
        - SELL: bid * (1 - cushion) で下げて踏みに行く
        cushion は eps_pct と paper_slip_bps を比較して大きい方を採用
        """
        if not getattr(self, "taker_mode", False):
            return candidate

        bid = getattr(self, "best_bid", None)
        ask = getattr(self, "best_ask", None)
        # eps_pct は float なので Decimal に正規化し、paper_slip_bps と比較
        eps_dec = Decimal(str(getattr(self, "eps_pct", 0) or 0))
        slip_dec = (self.paper_slip_bps or Decimal("0")) / Decimal("10000")
        cushion = max(eps_dec, slip_dec)

        try:
            if side.upper() == "BUY" and ask is not None:
                return float(ask * (Decimal("1") + cushion))
            if side.upper() == "SELL" and bid is not None:
                return float(bid * (Decimal("1") - cushion))
        except Exception:
            return candidate

        return candidate


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
