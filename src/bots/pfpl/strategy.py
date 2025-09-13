from __future__ import annotations
import asyncio
from collections import deque
from typing import Any, Dict
from datetime import datetime
from pathlib import Path
import time
import uuid
from hl_core.utils.retry import retry_async
from os import getenv
import logging
import math
from hyperliquid.utils import types as hl_types
import hashlib
from hyperliquid.info import Info
from hyperliquid.utils import constants
from decimal import Decimal, ROUND_DOWN, getcontext

getcontext().prec = 28


log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# DataCollector: API / WS から生データを 1 s 間隔でキャッシュ更新する
# ------------------------------------------------------------------
class DataCollector:
    """Hyperliquid API / WebSocket を非同期に呼び出し、
    次のキーを self.cache に保持するクラス。

    cache = {
        "projectedFunding": 0.0,
        "oiLong": 0.0,
        "oiShort": 0.0,
        "pointsMultiplier": 1.0,
        "timestamp": 0.0,
    }
    """

    def __init__(self, api_client: Any, ws_client: Any, symbol: str) -> None:
        self.api = api_client  # REST 用
        self.ws = ws_client  # WebSocket 用
        self.cache: Dict[str, float] = {
            "projectedFunding": 0.0,
            "oiLong": 0.0,
            "oiShort": 0.0,
            "pointsMultiplier": 1.0,
            "timestamp": 0.0,
            "midPrice": 0.0,
            "blockTs": 0.0,  # 最新ブロックの timestamp (秒)
            "nextFundingTs": 0.0,  # 追加: 次回 Funding UNIX 時刻 (秒)
        }
        self.symbol = symbol
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _update_loop(self) -> None:
        """1 s ごとに REST / WS からデータを取得し cache を更新する。"""
        while not self._stop.is_set():
            try:
                # --- REST（Mainnet）--------------------------------
                # 例 "BTC"
                ctxs = await self._safe_call(self.api.get_asset_ctx)
                asset_ctxs = ctxs["assetCtxs"] if "assetCtxs" in ctxs else ctxs[1]
                log.debug("AssetCtx sample: %s", asset_ctxs[0])

                ctx = asset_ctxs[0]  # 対象コインだけ抽出

                mid_px = float(ctx["midPx"])
                oi_long = float(ctx["openInterest"])
                proj_funding = float(ctx["funding"])
                next_ts = float(ctx.get("nextFundingTime", 0.0))

                self.cache.update(
                    projectedFunding=proj_funding,
                    oiLong=oi_long,
                    oiShort=0.0,  # short 個別が無いので 0 で埋める
                    timestamp=asyncio.get_event_loop().time(),
                    nextFundingTs=next_ts,
                    midPrice=mid_px,
                )

            except Exception as exc:  # noqa: BLE001
                # 失敗してもループは継続
                log.warning("[DataCollector] warning: %s", exc)
            await asyncio.sleep(1)

    @retry_async(max_attempts=4, base_delay=0.3)
    async def _safe_call(self, func, *args, **kwargs):
        """awaitable (coroutine) を安全に実行し、失敗時は None を返す"""
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            log.exception("DataCollector _safe_call error: %s", e)
            return None

    # --------------------------------------------------------------
    # パブリック API
    # --------------------------------------------------------------
    async def start(self) -> None:
        """更新ループを非同期タスクとして起動する。"""
        if self._task is None:
            self._task = asyncio.create_task(self._update_loop())

    async def stop(self) -> None:
        """更新ループを安全に停止する。"""
        self._stop.set()
        if self._task:
            await self._task
            self._task = None


# ------------------------------------------------------------------
# FeatureStore: ζ 計算用リングバッファを保持する
# ------------------------------------------------------------------
class FeatureStore:
    """30日分の ζ 履歴と PnL σ を保持するクラス。
    - update()     : 最新 ζ と任意 PnL をプッシュ
    - percentile_rank(): ζ の百分位 (0–100)
    - pnl_sigma()  : PnL の標準偏差 σ
    """

    WINDOW_SEC = 30 * 24 * 60 * 60  # 30 日

    def __init__(self) -> None:
        self.zeta_hist: deque[float] = deque(maxlen=self.WINDOW_SEC)
        self.pnl_hist: deque[float] = deque(maxlen=self.WINDOW_SEC)

    def update(self, zeta: float, pnl: float | None = None) -> None:
        self.zeta_hist.append(zeta)
        if pnl is not None:
            self.pnl_hist.append(pnl)

    def percentile_rank(self, zeta: float) -> float:
        if not self.zeta_hist:
            return 0.0
        below = sum(1 for v in self.zeta_hist if v <= zeta)
        return (below / len(self.zeta_hist)) * 100.0

    def pnl_sigma(self) -> float:
        n = len(self.pnl_hist)
        if n < 2:
            return 0.0
        mean = sum(self.pnl_hist) / n
        var = sum((x - mean) ** 2 for x in self.pnl_hist) / (n - 1)
        return math.sqrt(var)


# ------------------------------------------------------------------
# ScoringEngine: ζ 百分位と PnL σ を取得して意思決定材料を返す
# ------------------------------------------------------------------
class ScoringEngine:
    """FeatureStore と連携してスコアを計算するクラス。"""

    def __init__(self, store: FeatureStore) -> None:
        self.store = store

    def score(self, zeta: float) -> dict[str, float]:
        """現在 ζ に対する
        - percentile : ζ_pctl (0–100)
        - pnl_sigma  : σ_PnL
        を dict で返す。
        """
        return {
            "percentile": self.store.percentile_rank(zeta),
            "pnl_sigma": self.store.pnl_sigma(),
        }


# ------------------------------------------------------------------
# SignalGenerator: 逆張りエントリ / 決済判定を行う
# ------------------------------------------------------------------
class SignalGenerator:
    """ScoringEngine の出力と現在ポジションを参照して
    'ENTER', 'EXIT', または None を返す。
    """

    def __init__(self, scorer: ScoringEngine, store: FeatureStore) -> None:
        self.scorer = scorer
        self.store = store
        self._in_position: bool = False

    def evaluate(
        self, zeta: float, funding_paid: int, oi_bias: float, pnl: float
    ) -> str | None:
        """ルール:
        - ζ_pctl ≥ 95 かつ未ポジ → 'ENTER'
        - fundingPaid ≥3 または |oi_bias| が中央値未満 または PnL ≥ 2σ → 'EXIT'
        """
        scores = self.scorer.score(zeta)
        percentile = scores["percentile"]
        sigma = scores["pnl_sigma"]

        if not self._in_position and percentile >= 95.0:
            self._in_position = True
            return "ENTER"

        if self._in_position:
            if funding_paid >= 3 or abs(oi_bias) < 0.0 or pnl >= 2 * sigma:
                self._in_position = False
                return "EXIT"

        return None


# ------------------------------------------------------------------
# PositionManager: レバレッジ・サイズ計算とポジション状態の保持
# ------------------------------------------------------------------
class PositionManager:
    """ポジション入出管理。
    - enter(): エントリサイズ・レバを計算し state をセット
    - exit() : state をクリアしてクローズ要求を返す
    """

    def __init__(self, account_equity: float, target_lev: float = 2.5) -> None:
        self.equity = account_equity
        self.target_lev = target_lev
        self.state: dict[str, float] | None = None  # size, avg_px, ζ_at_entry

    def enter(
        self, side: str, price: float, notional_usd: float, zeta: float
    ) -> dict[str, float]:
        size = notional_usd / price
        self.state = {"side": side, "size": size, "avg_px": price, "zeta": zeta}
        return {"action": "OPEN", "side": side, "size": size}

    def exit(self) -> dict[str, float] | None:
        if not self.state:
            return None
        close_order = {
            "action": "CLOSE",
            "side": "SELL" if self.state["side"] == "BUY" else "BUY",
            "size": self.state["size"],
        }
        self.state = None
        return close_order


# ------------------------------------------------------------------
# ExecutionGateway: 発注・キャンセルを hl_core 経由で非同期実行
# ------------------------------------------------------------------
class ExecutionGateway:
    """place() / cancel() を提供し、latency ≤50 ms を目指す。"""

    def __init__(self, tx_client, symbol: Any) -> None:
        self.symbol = symbol
        self.tx = tx_client  # hl_core.execution client
        self._futures: dict[str, asyncio.Future] = {}

    async def _get_equity_ratio(self) -> float:
        # 役割: 口座のUSDC等価残高を取得し、1回の注文額(order_usd)に対する充足率(0.0..1.0)を返す。
        #      DRY-RUN時は常に1.0。SDKの仕様差に備えて複数メソッド(user_state/account/user/balances)へフォールバックする。
        if getattr(self, "dry_run", False):
            return 1.0

        order_usd = float(getattr(self, "order_usd", 15.0))
        try:
            loop = asyncio.get_running_loop()
            info = Info(
                getattr(self.tx, "base_url", constants.MAINNET_API_URL), skip_ws=True
            )  # 役割: tx_clientと同じURLで口座情報を取得（mainnet/testnet自動追従）
            addr = self.tx.wallet.address

            # 返却形が違っても拾えるように順に試す
            data = None
            for fetch in (
                lambda: info.user_state(addr),
                lambda: info.account(addr),
                lambda: info.user(addr),
                lambda: info.balances(addr),
            ):
                try:
                    data = await loop.run_in_executor(None, fetch)
                    if data:
                        break
                except Exception:
                    continue

            equity = 0.0
            # marginSummary / crossMarginSummary / balances(配列) の順で抽出
            try:
                equity = float((data or {}).get("marginSummary", {}).get("equity", 0.0))
            except Exception:
                pass
            if not equity:
                try:
                    equity = float(
                        (data or {}).get("crossMarginSummary", {}).get("equity", 0.0)
                    )
                except Exception:
                    pass
            if not equity and isinstance(data, list):
                for b in data:
                    if (b or {}).get("coin") == "USDC":
                        equity = float(b.get("available") or b.get("total") or 0.0)
                        break

            if equity <= 0.0 or order_usd <= 0.0:
                return 0.0
            return max(0.0, min(1.0, equity / order_usd))
        except Exception:
            return 0.0

    async def place(self, side: str, size: float, price: float) -> dict[str, Any]:
        usd = getattr(self, "order_usd", 15.0)
        loop = asyncio.get_running_loop()
        if not hasattr(self, "qty_tick"):
            info = Info(
                getattr(self.tx, "base_url", constants.MAINNET_API_URL), skip_ws=True
            )  # 役割: tx_clientと同じURLでmeta取得（環境ズレ防止）
            meta = await loop.run_in_executor(None, info.meta)
            unit = next(
                (u for u in meta["universe"] if u.get("name") == self.symbol), None
            )
            self.qty_tick = (
                10 ** (-unit["szDecimals"])
                if unit and ("szDecimals" in unit)
                else 0.001
            )
            qty_tick = self.qty_tick  # ここから下はこの刻みに合わせて計算する
            log.debug(
                "TICK-INFO %s szDecimals=%s qty_tick=%s",
                self.symbol,
                unit.get("szDecimals") if unit else None,
                qty_tick,
            )

        if (float(size) <= 0) and (float(price) > 0) and (usd > 0):
            size = usd / float(price)
        tick_dec = Decimal(str(qty_tick))
        qty_tick = getattr(
            self, "qty_tick", 10 ** (-3)
        )  # 何をする行か: 既にself.qty_tickがある場合でもローカルqty_tickを必ず定義（未定義エラー回避）

        units = (Decimal(str(size)) / tick_dec).to_integral_value(rounding=ROUND_DOWN)
        size_dec = units * tick_dec
        if size_dec < tick_dec:
            size_dec = tick_dec
        size_coin = float(size_dec.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))

        cloid = "0x" + hashlib.sha256(str(uuid.uuid4()).encode()).digest()[:16].hex()
        fut = self._futures[cloid] = asyncio.get_event_loop().create_future()

        size_coin = (
            round(float(size) / float(price), 8)
            if float(size) > 1 and float(price) > 100
            else round(float(size), 8)
        )
        qty_tick = self.qty_tick if getattr(self, "qty_tick", None) else 10 ** (-3)
        # 価格の最小単位（小数桁）を一時取得するが、この関数内では参照しないため意図的に未使用として保持
        # _px_decimals = market_meta["pxDecimals"]  # 削除: market_meta 未定義のため

        size_coin = (
            float(size)
            if float(size) > 0
            else ((10.0 / float(price)) if float(price) > 0 else qty_tick)
        )
        size_coin = max(size_coin, qty_tick)
        size_coin = math.floor(size_coin / qty_tick) * qty_tick
        min_sz_for_ntl = (
            math.ceil((10.0 / float(price)) / qty_tick) * qty_tick
        )  # 最低建玉$10を保証
        size_coin = max(size_coin, min_sz_for_ntl)
        px_tick = getattr(self, "px_tick", None)
        if px_tick is None:
            try:
                info = Info(
                    getattr(self.tx, "base_url", constants.MAINNET_API_URL),
                    skip_ws=True,
                )  # 役割: tx_clientと同じURLで口座/メタを取得（_get_equity_ratio / place / close / cancel_all いずれの箇所でも同じ置換）
                meta = await loop.run_in_executor(None, info.meta)
                unit = next(
                    (u for u in meta["universe"] if u.get("name") == self.symbol), None
                )
                self.px_tick = float(unit.get("pxTick", 0.5)) if unit else 0.5
            except Exception:
                self.px_tick = 0.5
        px_tick = self.px_tick
        int_digits = len(str(int(abs(float(price)))))
        if int_digits >= 5:
            # 整数は常に許可される（有効桁数制限の例外）。高価格帯では小数をやめて整数にする。
            limit_px = float(int(float(price)))
        else:
            # 小数が許可される価格帯のみ、px_tick の倍数に切り捨て
            limit_px = float(
                (Decimal(str(price)) / Decimal(str(px_tick))).to_integral_value(
                    rounding=ROUND_DOWN
                )
                * Decimal(str(px_tick))
            )

        log.debug(
            "PLACE-CALL side=%s size_in=%s price_in=%s -> sz=%s tick=%s min_ntl=%s limit_px=%s",
            side,
            size,
            price,
            size_coin,
            qty_tick,
            min_sz_for_ntl,
            limit_px,
        )

        px_tick = getattr(
            self, "px_tick", None
        )  # 何をする行か: place内でも価格刻み(px_tick)が未定義なら取得フローに入る
        if (
            px_tick is None
        ):  # 何をする行か: 初回だけ Info.meta() から該当シンボルの pxTick を取得してキャッシュ
            try:
                info = Info(
                    getattr(self.tx, "base_url", constants.MAINNET_API_URL),
                    skip_ws=True,
                )  # 何をする行か: tx_clientと同じURLでInfo初期化（環境ズレ防止）
                meta = await asyncio.get_running_loop().run_in_executor(
                    None, info.meta
                )  # 何をする行か: メタ情報(universe)を取得
                unit = next(
                    (u for u in meta["universe"] if u.get("name") == self.symbol), None
                )  # 何をする行か: 対象シンボルのユニット情報を抽出
                self.px_tick = (
                    float(unit.get("pxTick", 0.5)) if unit else 0.5
                )  # 何をする行か: 見つかればpxTick、無ければ0.5を既定に
            except Exception:
                self.px_tick = 0.5  # 何をする行か: 例外時のフォールバック
        px_tick = (
            self.px_tick
        )  # 何をする行か: 以降の丸め処理はキャッシュ済みのpx_tickを使用

        limit_px = float(
            (
                Decimal(str(limit_px * (1 + 0.0005 if side == "BUY" else 1 - 0.0005)))
                / Decimal(str(px_tick))
            ).to_integral_value(rounding=ROUND_DOWN)
            * Decimal(str(px_tick))
        )  # 役割: エントリー価格を±0.05%だけ乖離方向へ微調整し、px_tickに再丸め（fill率改善）

        await self.tx.place_order(
            coin=self.symbol,  # 銘柄名（例: "BTC"）— SDKはベース銘柄名で受け取る
            is_buy=(side == "BUY"),  # True=買い / False=売り
            sz=size_coin,  # 刻み( szDecimals )に合わせて切り捨て + 最低名目$10を満たした最終サイズ
            limit_px=limit_px,  # 銘柄の許容小数桁に丸めた指値（px_decimalsに基づく）
            order_type={
                "limit": {"tif": "Gtc"}
            },  # 指値 + GTC（キャンセルされるまで有効）
            reduce_only=False,  # 新規/追加の発注なので False（手仕舞い側 close は True にする）
            cloid=hl_types.Cloid(cloid),
        )

        return (
            cloid,
            fut,
        )  # 何をする行か: userFillsのclientOidと一致させるため、返すIDもcloidに統一

    async def close(self, side: str, size: float, price: float) -> dict[str, Any]:
        cid = str(uuid.uuid4())
        fut = self._futures[cid] = asyncio.get_event_loop().create_future()
        size_coin = (
            round(float(size) / float(price), 8)
            if float(size) > 1 and float(price) > 100
            else round(float(size), 8)
        )
        cid = str(uuid.uuid4())
        fut = self._futures[cid] = asyncio.get_event_loop().create_future()
        size_coin = (
            round(float(size) / float(price), 8)
            if float(size) > 1 and float(price) > 100
            else round(float(size), 8)
        )
        qty_tick = self.qty_tick if getattr(self, "qty_tick", None) else 10 ** (-3)
        # 価格の最小単位（小数桁）を一時取得するが、このスコープでは参照しないため意図的に未使用として保持

        size_coin = (
            float(size)
            if float(size) > 0
            else ((10.0 / float(price)) if float(price) > 0 else qty_tick)
        )
        size_coin = max(size_coin, qty_tick)
        size_coin = math.floor(size_coin / qty_tick) * qty_tick
        min_sz_for_ntl = (
            math.ceil((10.0 / float(price)) / qty_tick) * qty_tick
        )  # 最低建玉$10を保証
        size_coin = max(size_coin, min_sz_for_ntl)
        px_tick = getattr(self, "px_tick", None)
        if px_tick is None:
            try:
                info = Info(
                    getattr(self.tx, "base_url", constants.MAINNET_API_URL),
                    skip_ws=True,
                )  # 役割: tx_clientと同じURLでmeta取得（環境ズレ防止）
                loop = asyncio.get_running_loop()
                meta = await loop.run_in_executor(None, info.meta)
                unit = next(
                    (u for u in meta["universe"] if u.get("name") == self.symbol), None
                )
                self.px_tick = float(unit.get("pxTick", 0.5)) if unit else 0.5
            except Exception:
                self.px_tick = 0.5
        px_tick = self.px_tick
        int_digits = len(str(int(abs(float(price)))))
        if int_digits >= 5:
            # 整数は常に許可される（有効桁数制限の例外）。高価格帯では小数をやめて整数にする。
            limit_px = float(int(float(price)))
        else:
            # 小数が許可される価格帯のみ、px_tick の倍数に切り捨て
            limit_px = float(
                (Decimal(str(price)) / Decimal(str(px_tick))).to_integral_value(
                    rounding=ROUND_DOWN
                )
                * Decimal(str(px_tick))
            )

        log.debug(
            "CLOSE-CALL side=%s size_in=%s price_in=%s -> sz=%s tick=%s limit_px=%s",
            side,
            size,
            price,
            size_coin,
            qty_tick,
            limit_px,
        )

        await self.tx.place_order(
            coin=self.symbol,  # 銘柄（例: "BTC"）— SDKはベース銘柄名で受け取る
            is_buy=(side == "BUY"),  # True=買い / False=売り
            sz=size_coin,  # 刻みに合わせた最終数量（最低$10ノッチも満たす）
            limit_px=limit_px,  # 許容小数桁に丸めた指値
            order_type={"limit": {"tif": "Gtc"}},  # 指値 + GTC
            reduce_only=True,  # 手仕舞いなので必ず True（新規建てを防ぐ）
            cloid=hl_types.Cloid(
                "0x" + hashlib.sha256(str(cid).encode()).digest()[:16].hex()
            ),  # 0x + 16バイトhex
        )

        return cid, fut

    async def cancel_all(self) -> None:
        """未約定注文を全キャンセル。"""
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        info = Info(
            getattr(self.tx, "base_url", constants.MAINNET_API_URL), skip_ws=True
        )  # 役割: tx_clientと同じURLで口座/メタを取得（_get_equity_ratio / place / close / cancel_all いずれの箇所でも同じ置換）
        loop = asyncio.get_running_loop()
        open_orders = await loop.run_in_executor(
            None, info.open_orders, self.tx.wallet.address
        )
        for oo in open_orders:
            await loop.run_in_executor(None, self.tx.cancel, oo["coin"], oo["oid"])

    async def flatten_all(self):
        """全ポジション解消のキルスイッチ（最小版）: まず全注文をキャンセルして戻る"""
        await self.cancel_all()
        return

    # ── private fills WS を登録・待機 ───────────────────
    def attach_ws(self, ws_client) -> None:
        """WSClient を渡すと内部で fills を listen し、future を解決する。"""
        self.ws = ws_client
        self._futures: dict[str, asyncio.Future] = {}

        async def _on_msg(msg):
            if msg.get("channel") == "userFills":
                cid = msg["data"].get("clientOid")
                fut = self._futures.pop(cid, None)
                if fut and not fut.done():
                    fut.set_result(float(msg["data"]["price"]))

        # 既存の fanout を奪わないよう wrap する
        orig = getattr(self.ws, "on_message", None)

        async def _wrap(msg):
            await _on_msg(msg)
            if orig:
                await orig(msg)

        self.ws.on_message = _wrap

    async def wait_fill(self, order_id: str, timeout: float = 5.0) -> float:
        """order_id が fill されるまでポーリングし、fillPx を返す。
        timeout 秒を超えたら 0.0 を返して呼び元で判断。
        """
        t0 = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - t0 < timeout:
            status = await self.tx.get_order_status(order_id=order_id)
            if status.get("status") == "filled":
                return float(status["avgPx"])
            await asyncio.sleep(0.2)
        return 0.0

    def _log_trade_csv(
        logger_obj,
        symbol: str,
        side: str,
        size: float,
        price: float,
        reason: str,
        kind: str = "ENTRY",
    ):
        """何をする関数なのか: AnalysisLogger（analysis_logger.py）等の既存ロガーを使い、
        ts_iso,unix_ms,symbol,side,size,price,reason の1行CSV形式テキストをそのまま出力するアダプタ。
        logger.info(...) があればそれで出す。無ければ append 等を順に試し、最悪は print にフォールバックする。
        """
        from datetime import (
            datetime,
            timezone,
        )  # 関数内でだけ使う（外部依存を増やさない）

        # 何をする行か: CSVの1行分（ヘッダ互換）を組み立てる
        ts = datetime.now(timezone.utc)
        ts_iso = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        unix_ms = int(ts.timestamp() * 1000)
        line = f"{ts_iso},{unix_ms},{symbol},{side},{float(size):.8f},{float(price):.2f},{kind}:{reason}"

        # 何をする行か: 代表的なメソッド名を優先順で試す → 無ければ print
        if hasattr(logger_obj, "info"):
            logger_obj.info(line)
        elif hasattr(logger_obj, "append"):
            logger_obj.append(line)
        elif hasattr(logger_obj, "write"):
            logger_obj.write(line)
        else:
            print(line)


# ------------------------------------------------------------------
# RiskGuards: エクスポージャ・DD・遅延などを監視し Kill-Switch
# ------------------------------------------------------------------
class RiskGuards:
    """run() を定期呼び出しし、条件を満たせば例外で停止させる。"""

    def __init__(
        self,
        max_pos_usd: float = 300.0,
        max_equity_ratio: float = 0.20,
        max_daily_dd: float = 0.03,
        max_block_delay: float = 6.0,
    ) -> None:
        self.max_pos_usd = max_pos_usd
        self.max_equity_ratio = max_equity_ratio
        self.max_daily_dd = max_daily_dd
        self.max_block_delay = max_block_delay

    def run(
        self,
        pos_usd: float,
        equity_ratio: float,
        daily_dd: float,
        block_delay: float,
    ) -> None:
        if (
            abs(pos_usd) > self.max_pos_usd
            or equity_ratio > self.max_equity_ratio
            or daily_dd > self.max_daily_dd
            or block_delay > self.max_block_delay
        ):
            raise RuntimeError("RiskGuards tripped — strategy disabled.")


# ------------------------------------------------------------------
# AnalysisLogger: OPEN / CLOSE 行を CSV へ出力
# ------------------------------------------------------------------
class AnalysisLogger:
    """PFPL 専用シンプル CSV ロガー。"""

    def __init__(self, path: str = "logs/pfpl/pfpl_trades.csv") -> None:
        """PFPL 専用 CSV を logs/pfpl/ に必ず残す。"""
        self.path = path
        # logs/ と logs/pfpl/ を自動生成
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # ファイルが無ければヘッダーを書いて作成
        if not Path(self.path).exists():
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("ts_iso,side,size,price,pnl_usd\n")

    def log_open(self, side: str, size: float, price: float) -> None:
        ts_iso = datetime.utcnow().isoformat()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"{ts_iso},{side},{size},{price},\n")

    def log_close(self, pnl_usd: float) -> None:
        ts_iso = datetime.utcnow().isoformat()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"{ts_iso},CLOSE,,,{pnl_usd}\n")


# エントリポイントは後続ステップで実装する
async def run_live(api_client, ws_client, tx_client) -> None:
    """PFPL v2 フルフロー非同期ループ（簡易版）"""
    # ── 初期化 ─────────────────────────────────────
    collector = DataCollector(api_client, ws_client, "BTC-PERP")

    # --- 口座残高 USD を取得 ------------------------------------

    account_equity = await api_client.get_equity(getenv("HL_ACCOUNT_ADDR"), perp=True)
    log.info("Equity loaded = %.2f USDC", account_equity)

    await collector.start()

    store = FeatureStore()
    scorer = ScoringEngine(store)
    signaler = SignalGenerator(scorer, store)
    pos_mgr = PositionManager(account_equity)
    symbol = "BTC"  # 取引対象ペア
    exec_gate = ExecutionGateway(tx_client, symbol)
    exec_gate.order_usd = float(
        locals().get("order_usd")
        or (
            locals().get("pair")
            or locals().get("pair_cfg")
            or locals().get("params")
            or {}
        ).get("order_usd")
        or 15.0
    )  # 役割: CLI/YAMLなどの order_usd を ExecutionGateway に伝播
    exec_gate.dry_run = bool(
        locals().get("dry_run")
        or (locals().get("params") or {}).get("dry_run")
        or getattr(exec_gate, "dry_run", False)
    )  # 役割: DRY-RUNフラグを ExecutionGateway に伝播（DRY時は equity 取得をスキップ）
    exec_gate.use_market_close = bool(
        (locals().get("params") or {}).get("use_market_close")
        or (locals().get("pair_cfg") or {}).get("use_market_close")
        or locals().get("use_market_close")
        or False
    )  # 役割: 決済を成行/指値で切替できるようフラグを伝播

    equity_ratio = (
        await exec_gate._get_equity_ratio()
    )  # 役割: 残高充足率(0..1)を取得（order_usd 反映後）
    if equity_ratio < 1.0:
        print(
            f"GATE-SKIP equity<order_usd: ratio={equity_ratio:.2f} order_usd={getattr(exec_gate, 'order_usd', 0.0):.2f}"
        )
        return  # 役割: 残高が注文額に満たないので今回は発注しない
    print(
        f"EQUITY-CHECK ratio={equity_ratio:.2f} order_usd={getattr(exec_gate, 'order_usd', 0.0):.2f}"
    )  # 役割: 残高比率と注文額をログ出力（デバッグ）

    exec_gate.attach_ws(ws_client)
    guards = RiskGuards()
    logger = AnalysisLogger()
    log.info("Logger init OK → %s", logger.path)

    # ── 日次 DD 用初期化 ───────────────────────────────
    daily_start_equity = account_equity  # その日の起点 Equity
    start_day_utc = datetime.utcnow().date()  # 今日の日付 (UTC)

    funding_paid = 0  # Funding 支払い回数
    last_next_ts = 0.0  # 直前に観測した nextFundingTs

    try:
        while True:
            # 日付が変わったら起点 Equity をリセット
            today_utc = datetime.utcnow().date()
            if today_utc != start_day_utc:
                daily_start_equity = await api_client.get_equity(
                    getenv("HL_ACCOUNT_ADDR"), perp=True
                )

                start_day_utc = today_utc

            # 現在の Equity を取得
            current_equity = await api_client.get_equity(
                getenv("HL_ACCOUNT_ADDR"), perp=True
            )

            # ① 特徴量計算
            c = collector.cache
            oi_bias = math.log(c["oiLong"] / c["oiShort"]) if c["oiShort"] else 0.0
            fund_sign = (
                math.copysign(1, c["projectedFunding"])
                if c["projectedFunding"]
                else 0.0
            )
            zeta = abs(oi_bias) * fund_sign * c["pointsMultiplier"]

            # Funding 支払い検知
            next_ts = collector.cache.get("nextFundingTs", 0.0)
            if pos_mgr.state and next_ts and next_ts != last_next_ts:
                funding_paid += 1
                last_next_ts = next_ts

            # ② ストア更新 & スコア取得
            store.update(zeta)
            # ζ と百分位をデバッグ出力
            log.debug(
                "ζ = %.4f, pctl = %.1f", zeta, signaler.scorer.score(zeta)["percentile"]
            )

            signal = signaler.evaluate(zeta, funding_paid, oi_bias, pnl=0.0)

            # PnL は後続実装

            # ③ シグナル処理
            if signal == "ENTER":
                funding_paid = 0  # 新規ポジションでリセット
                side = "SELL" if fund_sign > 0 else "BUY"

                # --- 最新 midPrice を取得（0.0なら 0.5s 待機） ---
                price = collector.cache.get("midPrice") or 0.0
                log.debug("midPrice debug: %s", price)
                if price == 0.0:  # mid がまだ無いなら 0.5 秒待機
                    await asyncio.sleep(0.5)
                    continue

                notional = min(account_equity * 0.1, 5_000)  # 残高の10%か5k USD
                order = pos_mgr.enter(
                    side, price=price, notional_usd=notional, zeta=zeta
                )

                cid, fut = await exec_gate.place(order["side"], order["size"], price)
                fill_px = await fut
                log.info(
                    "ENTRY fill: side=%s size=%.8f px=%.2f",
                    order["side"],
                    float(order["size"]),
                    float(fill_px),
                )  # 何をする行か: 可読ログ（CSVは logger.log_open で既に出力）

                pos_mgr.state["avg_px"] = fill_px
                logger.log_open(order["side"], order["size"], fill_px)

            elif signal == "EXIT":
                if not pos_mgr.state:
                    continue
                entry_px = pos_mgr.state["avg_px"]
                entry_size = pos_mgr.state["size"]

                order = pos_mgr.exit()
                cid, fut = await exec_gate.close(order["side"], order["size"], price)
                fill_px = await fut
                pnl_usd = (fill_px - entry_px) * entry_size
                logger.log_close(pnl_usd=pnl_usd)

            # ④ リスクガード（簡易）
            try:
                pos_usd = (pos_mgr.state["size"] * price) if pos_mgr.state else 0.0
                equity_ratio = abs(pos_usd) / account_equity if account_equity else 0.0
                daily_dd = max(0.0, daily_start_equity - current_equity)

                block_delay = abs(
                    time.time() - collector.cache.get("blockTs", time.time())
                )

                from hyperliquid.info import Info
                from hyperliquid.utils import constants

                loop = asyncio.get_running_loop()
                info = Info(
                    getattr(tx_client, "base_url", constants.MAINNET_API_URL),
                    skip_ws=True,
                )  # 役割: run_live内でInfoをExchange(tx_client)と同じURLで初期化
                state = await loop.run_in_executor(
                    None, info.user_state, tx_client.wallet.address
                )
                equity = float(state["marginSummary"]["accountValue"])
                if equity <= 0:
                    log.debug("GATE-SKIP equity<=0: equity=%.2f USDC", equity)

                    await asyncio.sleep(1.0)
                    continue

                guards.run(
                    pos_usd=pos_usd,
                    equity_ratio=equity_ratio,
                    daily_dd=daily_dd,
                    block_delay=block_delay,
                )
            except RuntimeError as e:
                log.critical("%s — Kill-Switch", e)
                await exec_gate.cancel_all()  # ① 注文をすべてキャンセル
                await exec_gate.flatten_all()  # ② 全ポジションを反対売買で解消
                await ws_client.close()  # ③ WebSocket 切断
                await api_client.close()  # ④ REST 切断
                break  # ⑤ ループ終了

    finally:
        await collector.stop()


class PFPLStrategy:
    """レガシー用ラッパー（tests / run_bot が期待するインターフェース）。

    実際のロジックは run_pfpl で動かすため、
    ここではコンストラクタと on_message の形だけ保持。
    """

    def __init__(
        self, config: dict[str, Any], semaphore: Any, sdk: Any | None = None
    ) -> None:

        self.config = config
        self.semaphore = semaphore
        self.sdk = sdk
        self._last_price = 0.0  # 最新ミッド価格
        self._last_funding = 0.0  # 次回 Funding 率
        self._last_zeta = 0.0
        self.next_funding_ts = 0.0  # 次回 Funding 時刻 (s)
        self.funding_paid = 0  # Funding 支払済み回数
        self._last_block_ts = 0.0  # 直近 Block タイムスタンプ (s)
        self._in_position = False  # ポジション有無フラグ

        self._store = FeatureStore()  # ζ 履歴バッファ
        self._scorer = ScoringEngine(self._store)

    def on_message(self, msg: dict) -> None:
        """WS 受信メッセージを取り込み、最新価格・Funding・ζ を更新。"""
        ch = msg.get("channel") or msg.get("type")

        # ── Best-Bid/Ask → mid 価格 ─────────────────────────────
        if ch == "bbo":
            data = msg["data"]

            # Hyperliquid v2 では bbo が配列形式 ([bestBid, bestAsk])
            arr = data.get("bbo")
            if arr and isinstance(arr, list) and len(arr) >= 2:
                bid = float(arr[0]["px"])
                ask = float(arr[1]["px"])
            else:  # 旧フォーマット {bidPx, askPx} にも対応
                bid = float(data["bidPx"])
                ask = float(data["askPx"])

            self._last_price = (bid + ask) / 2

        # ── Funding 情報 ─────────────────────────────────────
        elif ch in ("fundingInfo", "fundingInfoTestnet"):
            info = msg["data"].get("BTC-PERP")
            if info:
                new_ts = float(info.get("nextFundingTime", 0))
                # 次回 Funding 時刻が更新されたら「支払い済み」とみなす
                if self.next_funding_ts and new_ts != self.next_funding_ts:
                    self.funding_paid += 1
                self.next_funding_ts = new_ts
                self._last_funding = float(
                    info.get("nextFundingRate") or info.get("projectedFunding") or 0.0
                )
                self._recompute_zeta()

        # ── OI 情報 (例: custom feed 'openInterest') ───────────
        elif ch == "openInterest":
            data = msg["data"]
            self._oi_long = float(data.get("oiLong", 0))
            self._oi_short = float(data.get("oiShort", 0))
            self._recompute_zeta()
        # ── Testnet 用: activeAssetCtx 1 本で OI / Funding / midPx が取れる ─
        elif ch == "activeAssetCtx":
            ctx = msg["data"]["ctx"]
            self._last_funding = float(ctx.get("funding", 0.0))
            self._oi_long = float(ctx["openInterest"])

            self._last_price = float(ctx.get("midPx", 0.0))
            self._recompute_zeta()

        # ── Block 情報 ─
        elif ch == "blocks":
            blk_ts = float(msg["data"]["timestamp"])
            self.block_delay = abs(time.time() - blk_ts)
            self._last_block_ts = blk_ts

    # ────────────────────────────────────────────────────────
    # 内部ユーティリティ
    # ────────────────────────────────────────────────────────
    def _recompute_zeta(self) -> None:
        """最新の OI / Funding から ζ を再計算し _last_zeta を更新。"""
        oi_long = getattr(self, "_oi_long", 0.0)
        oi_short = getattr(self, "_oi_short", 0.0)
        self._last_zeta = self._calc_zeta(
            oi_long=oi_long,
            oi_short=oi_short,
            projected_funding=self._last_funding,
            points_multiplier=1.0,  # 公開時のみ変更
        )
        self._store.update(self._last_zeta)  # ヒストリーに追加

    def _generate_signal(self) -> str | None:
        """最新 ζ 百分位に応じてシグナルを返す
        - 未ポジかつ pctl ≥ 95 → 'ENTER'
        - 保持中かつ pctl ≤ 50 → 'EXIT'
        """
        pctl = self.zeta_percentile

        if not self._in_position and pctl >= 95.0:
            self._in_position = True
            return "ENTER"

        if self._in_position and pctl <= 50.0:
            self._in_position = False
            return "EXIT"

        return None

    @property
    def last_price(self) -> float:
        """最新ミッド価格を返す。ユニットテスト用。"""
        return self._last_price

    @property
    def last_funding(self) -> float:
        """直近 Funding 率を返す。ユニットテスト用。"""
        return self._last_funding

    @property
    def last_zeta(self) -> float:
        """最新 ζ を返す。"""
        return self._last_zeta

    @property
    def funding_paid_count(self) -> int:
        return self.funding_paid

    @property
    def latest_block_delay(self) -> float:
        return getattr(self, "block_delay", 0.0)

    @property
    def zeta_percentile(self) -> float:
        """最新 ζ の百分位 (0-100)。"""
        return self._scorer.score(self._last_zeta)["percentile"]

    # ────────────── ζ を計算して返すユーティリティ ──────────────
    def _calc_zeta(
        self,
        oi_long: float,
        oi_short: float,
        projected_funding: float,
        points_multiplier: float = 1.0,
    ) -> float:
        """ζ = |log(oiLong / oiShort)| × sign(projectedFunding) × pointsMultiplier"""
        if oi_long <= 0 or oi_short <= 0:
            return 0.0
        oi_bias = math.log(oi_long / oi_short)
        fund_sign = math.copysign(1, projected_funding) if projected_funding else 0.0
        return abs(oi_bias) * fund_sign * points_multiplier

    # ───────── BUY/SELL 方向で ±オフセットした価格を返す ─────────
    def _price_with_offset(
        self,
        price: float,
        side: str,
        offset_pct: float | None = None,
    ) -> float:
        off = (
            float(offset_pct)
            if offset_pct is not None
            else float(self.config.get("eps_pct", 0.001))
        )
        return price * (1 - off) if side.upper() == "BUY" else price * (1 + off)

    # ───── Funding まで残り buffer_sec 秒以内なら True を返す ─────
    def _should_close_before_funding(
        self,
        current_ts: float | str,
        buffer_sec: float | None = None,
    ) -> bool:
        """Funding まで残り時間が 0 < Δt ≤ buffer_sec 秒なら True
        - buffer_sec を指定しなければ self.config['funding_close_buffer_secs'] を使用
        """
        # バッファ秒を決定
        if buffer_sec is None:
            buffer_sec = float(self.config.get("funding_close_buffer_secs", 600))

        try:
            now = float(current_ts)
        except (TypeError, ValueError):
            return False

        remaining = getattr(self, "next_funding_ts", 0) - now
        return 0 < remaining <= buffer_sec
