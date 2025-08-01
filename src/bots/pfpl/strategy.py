from __future__ import annotations
import asyncio
import math
from collections import deque
from typing import Any, Dict
from datetime import datetime
from pathlib import Path
import time
import uuid


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

    def __init__(self, api_client: Any, ws_client: Any) -> None:
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
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _update_loop(self) -> None:
        """1 s ごとに REST / WS からデータを取得し cache を更新する。"""
        while not self._stop.is_set():
            try:
                # --- REST 呼び出し例（擬似） ---
                funding = await self.api.get_projected_funding("BTC-PERP")
                oi_data = await self.api.get_open_interest("BTC-PERP")
                points = await self.api.get_points_multiplier()
                ticker = await self.api.get_ticker("BTC-PERP")  # ← mid 取得
                mid_px = float(ticker["mid"])  # best-bid/ask の平均
                funding = await self.api.get_projected_funding("BTC-PERP")
                blk = await self.api.get_latest_block()
                blk_ts = float(blk["timestamp"])
                next_ts = float(funding.get("nextFundingTime", 0))  # ← 追加

                self.cache.update(
                    projectedFunding=float(funding["projectedFunding"]),
                    oiLong=float(oi_data["oiLong"]),
                    oiShort=float(oi_data["oiShort"]),
                    pointsMultiplier=float(points or 1.0),
                    timestamp=asyncio.get_event_loop().time(),
                    midPrice=mid_px,
                    blockTs=blk_ts,
                    nextFundingTs=next_ts,
                )
            except Exception as exc:  # noqa: BLE001
                # 失敗してもループは継続
                print(f"[DataCollector] warning: {exc}")
            await asyncio.sleep(1)

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

    def __init__(self, tx_client: Any) -> None:
        self.tx = tx_client  # hl_core.execution client

    async def place(self, side: str, size: float, price: float) -> dict[str, Any]:
        cid = str(uuid.uuid4())
        fut = self._futures[cid] = asyncio.get_event_loop().create_future()
        await self.tx.place_order(
            symbol="BTC-PERP", side=side, size=size, px=price, clientOid=cid
        )
        return cid, fut

    async def close(self, side: str, size: float, price: float) -> dict[str, Any]:
        cid = str(uuid.uuid4())
        fut = self._futures[cid] = asyncio.get_event_loop().create_future()
        await self.tx.place_order(
            symbol="BTC-PERP", side=side, size=size, px=price, clientOid=cid
        )
        return cid, fut

    async def cancel_all(self) -> None:
        """未約定注文を全キャンセル。"""
        await self.tx.cancel_all(symbol="BTC-PERP")

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


# ------------------------------------------------------------------
# RiskGuards: エクスポージャ・DD・遅延などを監視し Kill-Switch
# ------------------------------------------------------------------
class RiskGuards:
    """run() を定期呼び出しし、条件を満たせば例外で停止させる。"""

    def __init__(
        self,
        max_pos_usd: float = 10_000.0,
        max_equity_ratio: float = 0.35,
        max_daily_dd: float = 300.0,
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

    def __init__(self, path: str = "pfpl_trades.csv") -> None:
        self.path = path
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
    collector = DataCollector(api_client, ws_client)
    # --- 口座残高 USD を取得 ------------------------------------
    account_equity = float(
        (await api_client.get_account_overview()).get("equityUsd", 0)
    )
    await collector.start()

    store = FeatureStore()
    scorer = ScoringEngine(store)
    signaler = SignalGenerator(scorer, store)
    pos_mgr = PositionManager(account_equity)
    exec_gate = ExecutionGateway(tx_client)
    exec_gate.attach_ws(ws_client)
    guards = RiskGuards()
    logger = AnalysisLogger()
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
                daily_start_equity = float(
                    (await api_client.get_account_overview()).get("equityUsd", 0)
                )
                start_day_utc = today_utc

            # 現在の Equity を取得
            current_equity = float(
                (await api_client.get_account_overview()).get(
                    "equityUsd", account_equity
                )
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
            signal = signaler.evaluate(zeta, funding_paid, oi_bias, pnl=0.0)

            # PnL は後続実装

            # ③ シグナル処理
            if signal == "ENTER":
                funding_paid = 0  # 新規ポジションでリセット
                side = "SELL" if fund_sign > 0 else "BUY"

                price = collector.cache.get("midPrice") or 0.0
                if price == 0.0:
                    await asyncio.sleep(1)
                    continue

                notional = min(account_equity * 0.1, 5_000)  # 残高の10%か5k USD
                order = pos_mgr.enter(
                    side, price=price, notional_usd=notional, zeta=zeta
                )

                cid, fut = await exec_gate.place(order["side"], order["size"], price)
                fill_px = await asyncio.wait_for(fut, timeout=5.0)
                pos_mgr.state["avg_px"] = fill_px
                logger.log_open(order["side"], order["size"], fill_px)

            elif signal == "EXIT":
                if not pos_mgr.state:
                    continue
                entry_px = pos_mgr.state["avg_px"]
                entry_size = pos_mgr.state["size"]

                order = pos_mgr.exit()
                cid, fut = await exec_gate.close(order["side"], order["size"], price)
                fill_px = await asyncio.wait_for(fut, timeout=5.0)
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

                guards.run(
                    pos_usd=pos_usd,
                    equity_ratio=equity_ratio,
                    daily_dd=daily_dd,
                    block_delay=block_delay,
                )
            except RuntimeError as e:
                print(e)
                break

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
