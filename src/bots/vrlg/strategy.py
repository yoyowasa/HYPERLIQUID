# 〔このモジュールがすること〕
# VRLG 戦略の司令塔。設定ロード、タスク起動/停止、シグナル処理の骨組みを提供します。
# 実際のデータ購読・シグナル検出・執行・リスク管理は後続ステップで実装して差し込みます。

from __future__ import annotations

import argparse
import asyncio
import contextlib  # 〔この import がすること〕 タイマータスクを安全にキャンセル（例外抑止）するために使います
import signal
import sys
import time  # 〔この import がすること〕 ブロック間隔の計算（秒）に使用します
from typing import Optional

# uvloop があれば高速化（なくても動く）
try:
    import uvloop  # type: ignore
except Exception:  # pragma: no cover
    uvloop = None  # type: ignore

# 〔この関数がすること〕: 共通ロガー/コンフィグ読込は PFPL と共有（hl_core）を使います。
import logging
from pathlib import Path
import logging.handlers
from hl_core.utils.logger import create_csv_formatter

# 〔この import 群がすること〕
# データ購読・位相検出・シグナル判定・発注・リスク管理の各コンポーネントを司令塔に読ませます。
from .rotation_detector import RotationDetector
from .signal_detector import SignalDetector
from .execution_engine import ExecutionEngine
from .risk_management import RiskManager
from .metrics import Metrics  # 〔この import がすること〕 Prometheus 送信ラッパを使えるようにする
from .config import (
    VRLGConfig,
    coerce_vrlg_config,
    load_vrlg_config,
)  # 〔この import がすること〕 dict設定を dataclass へ変換し、型ヒントと専用ローダーを利用する
from .data_feed import run_feeds, FeatureSnapshot  # 〔この import がすること〕 L2購読→100ms特徴量生成（run_feeds）と特徴量型を使えるようにする
from hl_core.utils.decision_log import DecisionLogger  # 〔この import がすること〕 共通ロガー（PFPL等と共有）を利用する
from .size_allocator import SizeAllocator  # 〔この import がすること〕 クリップサイズ算出ロジックを利用する


def load_config(path: str):
    """〔この関数がすること〕
    VRLG の設定をファイルから読み込み、必ず dataclass（VRLGConfig）で返します。
    ・実行時: load_vrlg_config を呼んで TOML/YAML を読み込み → dataclass 化
    ・テスト時: monkeypatch で strategy_mod.load_config を差し替え可能（dict を返されても後段の coerce で吸収）
    """

    return load_vrlg_config(path)


logger = logging.getLogger("bots.vrlg")


class VRLGStrategy:
    """〔このクラスがすること〕
    戦略全体のライフサイクルを管理します:
    - 設定を読み込む
    - キューや各コンポーネント（後で実装）を初期化する
    - 非同期タスク（データ→シグナル→執行）を起動/停止する
    - 将来、Prometheus のメトリクス公開を行う
    """


    def __init__(self, config_path: str, paper: bool, prom_port: Optional[int] = None, decisions_file: Optional[str] = None) -> None:  # 〔この行がすること〕 意思決定ログの出力先を受け取れるようにする

        """〔このメソッドがすること〕
        TOML/YAML 設定を読み込み、実行モード（paper/live）を保持します。
        """
        self.config_path = config_path
        self.paper = paper
        raw_cfg = load_config(config_path)
        self.cfg: VRLGConfig = coerce_vrlg_config(raw_cfg)
                # Attach a single per-symbol rotating file handler under logs/vrlg/<SYMBOL>.csv
        try:
            symbol = getattr(getattr(self.cfg, "symbol", object()), "name", None) or "UNSPEC"
        except Exception:
            symbol = "UNSPEC"
        log_dir = Path("logs") / "vrlg"
        log_dir.mkdir(parents=True, exist_ok=True)
        symbol_log_path = (log_dir / f"{symbol}.csv").resolve()
        existing_handler = next(
            (
                h
                for h in logger.handlers
                if isinstance(h, logging.handlers.TimedRotatingFileHandler)
                and getattr(h, "baseFilename", "") == str(symbol_log_path)
            ),
            None,
        )
        if existing_handler is None:
            fh = logging.handlers.TimedRotatingFileHandler(
                filename=str(symbol_log_path),
                when="midnight",
                interval=1,
                backupCount=14,
                encoding="utf-8",
                utc=False,
            )
            fh.setFormatter(create_csv_formatter(include_logger_name=True))
        logger.addHandler(fh)
        logger.propagate = False
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

        # 〔この属性がすること〕: 各段の非同期パイプ
        self.q_features: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self.q_signals: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self.decisions = DecisionLogger(filepath=decisions_file)  # 〔この行がすること〕 意思決定ログの出力を初期化
        self.metrics = Metrics(port=prom_port)  # 〔この行がすること〕 /metrics を起動し、以降の観測値を送れるようにする

        # 〔この属性がすること〕直近の特徴量を保持し、発注時に板消費率などの参照に使います。
        self._last_features: Optional[FeatureSnapshot] = None
        self._order_trace: dict[str, str] = {}  # 〔この行がすること〕 order_id → trace_id の対応を保持して、fills で trace_id を引けるようにする

        # 〔この属性がすること〕: 各コンポーネントの実体を生成し司令塔に保持します。
        self.rot = RotationDetector(self.cfg)
        self.sigdet = SignalDetector(self.cfg)
        # 〔この行がすること〕 シグナル判定のゲート評価を受け取り、メトリクス/意思決定ログへ反映できるようにする
        self.sigdet.on_gate_eval = self._on_gate_eval
        self.exe = ExecutionEngine(self.cfg, paper=self.paper)
        # 〔この行がすること〕 ExecutionEngine→Strategy へ発注イベントを通知するコールバックを配線
        self.exe.on_order_event = self._on_order_event
        self.risk = RiskManager(self.cfg)
        self.sizer = SizeAllocator(self.cfg)  # 〔この行がすること〕 発注直前に使うサイズ決定器を初期化

    async def start(self) -> None:
        """〔このメソッドがすること〕
        戦略の主要タスクを起動します。
        - data_feed: WS購読→100ms特徴量を q_features へ
        - _signal_loop: 特徴量→位相推定→シグナル判定→q_signals へ
        - _exec_loop: シグナル消費→発注/キャンセル/クローズ
        """
        logger.info("VRLG starting (paper=%s, cfg=%s)", self.paper, self.config_path)

        # 〔この行がすること〕WebSocket購読→100ms特徴量生成タスクを起動します。
        self._tasks.append(asyncio.create_task(run_feeds(self.cfg, self.q_features), name="feed_loop"))

        self._tasks.append(asyncio.create_task(self._signal_loop(), name="signal_loop"))
        self._tasks.append(asyncio.create_task(self._exec_loop(), name="exec_loop"))
        # 〔この行がすること〕 約定WSを購読し、滑り・クールダウン・メトリクスを更新するループを起動します
        self._tasks.append(asyncio.create_task(self._fills_loop(), name="fills_loop"))
        # 〔この行がすること〕 ブロックWS監視ループを起動して、ブロック間隔→Risk/Metrics/DecisionLogへ反映する
        self._tasks.append(asyncio.create_task(self._blocks_loop(), name="blocks_loop"))

    async def _signal_loop(self) -> None:
        """〔このメソッドがすること〕
        特徴量を受け取り、位相推定と 4 条件ゲートのシグナル判定を行い、
        条件合致時は q_signals に投入します。
        """
        while not self._stopping.is_set():
            try:
                feat = await self.q_features.get()
                # 〔このブロックがすること〕 RotationDetector から位相を計算し、特徴量に block_phase を埋め込みます
                phase = float(self.rot.current_phase(float(feat.t)))
                feat = feat.with_phase(phase)
                self._last_features = feat  # 〔この行がすること〕 スプレッド監視や滑り計算で使うため最新スナップショットを保存
                # 〔このブロックがすること〕 特徴量の「鮮度」を計算し、メトリクス更新＆しきい値超過なら処理をスキップします
                now_ts = time.time()
                feat_ts = float(feat.t)
                enforce_guard = feat_ts > 1e6  # 〔この条件がすること〕 epoch 秒である場合のみ鮮度ガードを有効化します
                staleness_ms = 0.0
                if enforce_guard:
                    staleness_ms = max(0.0, (now_ts - feat_ts) * 1000.0)
                self.metrics.set_data_staleness_ms(staleness_ms)
                max_stale = float(getattr(self.cfg.latency, "max_staleness_ms", 300))
                if enforce_guard and staleness_ms > max_stale:
                    self.metrics.inc_staleness_skips()
                    self.decisions.log("stale_feature", staleness_ms=float(staleness_ms), max_allowed_ms=float(max_stale))
                    continue
                self.metrics.observe_spread(float(feat.spread_ticks))  # 〔この行がすること〕 観測スプレッド（ticks）をヒストグラムへ
                self.rot.update(feat.t, feat.dob, feat.spread_ticks)
                self.metrics.set_period(self.rot.current_period() or 0.0)   # 〔この行がすること〕 推定された周期R*をGaugeへ
                # 〔このブロックがすること〕 周期検出の品質（score / p値 / サンプル数）をメトリクスに反映します
                try:
                    est = self.rot.last_estimation()
                except Exception:
                    est = None
                try:
                    self.metrics.set_rotation_quality(
                        score=float(getattr(est, "score", 0.0) if est is not None else 0.0),
                        n_boundary=int(getattr(est, "n_boundary", 0) if est is not None else 0),
                        p_dob=getattr(est, "p_dob", None) if est is not None else None,
                        p_spr=getattr(est, "p_spread", None) if est is not None else None,
                    )
                except Exception:
                    logger.debug("metrics.set_rotation_quality failed (ignored)")
                try:
                    self.metrics.set_active(bool(self.rot.is_active()))
                except Exception:
                    logger.debug("metrics.set_active failed (ignored)")
                if not self.rot.is_active():
                    continue
                phase = float(self.rot.current_phase(float(feat.t)))
                feat = feat.with_phase(phase)
                self.exe.set_period_hint(self.rot.current_period() or 1.0)
                # 〔この行がすること〕 推定R*に基づくクールダウン窓（秒）を Gauge に反映します
                try:
                    period = float(self.rot.current_period() or 1.0)
                    self.metrics.set_cooldown(self.exe.cooldown_factor * period)
                except Exception:
                    logger.debug("metrics.set_cooldown failed (ignored)")

                sig = self.sigdet.update_and_maybe_signal(float(feat.t), feat)
                if sig:
                    # 〔このブロックがすること〕 R*（周期検出）が非アクティブの間は執行せずスキップする
                    if not self.rot.is_active():
                        self.decisions.log("rotation_paused", reason="inactive", trace_id=getattr(sig, "trace_id", None))
                        continue
                    self.decisions.log(
                        "signal",
                        phase=phase,
                        spread_ticks=float(feat.spread_ticks),
                        dob=float(feat.dob),
                        obi=float(feat.obi),
                        trace_id=sig.trace_id,
                    )  # 〔この行がすること〕 シグナルの根拠となる特徴量を記録
                    self.metrics.inc_signals(1)  # 〔この行がすること〕 RotationがActiveのもとで立ったシグナルを1件カウントする
                    self.metrics.inc_signal()  # 〔この行がすること〕 シグナル発火回数をカウントアップ
                    await self.q_signals.put(sig)
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover
                logger.exception("signal_loop error: %s", e)

    def _on_order_event(self, kind: str, fields: dict) -> None:
        """〔この関数がすること〕
        ExecutionEngine からのオーダーイベントを受け取り、意思決定ログとメトリクスへ反映します。
        kind: 'skip' | 'submitted' | 'reject' | 'cancel'
        """
        # 1) 意思決定ログへ（order_skip / order_submitted / order_reject / order_cancel のイベント名で統一）
        try:
            self.decisions.log(f"order_{kind}", **fields)
        except Exception:
            pass

        # 〔このブロックがすること〕 "reject"/"cancel" の件数をメトリクスへ加算（運用監視用）
        try:
            if kind == "reject":
                self.metrics.inc_orders_rejected(1)
            elif kind == "cancel":
                self.metrics.inc_orders_canceled(1)
        except Exception:
            pass


        # 〔このブロックがすること〕 open_maker_btc が含まれていれば Gauge を更新
        try:
            if "open_maker_btc" in fields:
                self.metrics.set_open_maker_btc(float(fields["open_maker_btc"]))
        except Exception:
            pass

        # 〔このブロックがすること〕 "submitted" 受信時に板消費率（display/TopDepth）を登録し、Gauge を更新します
        if kind == "submitted":
            try:
                disp = float(fields.get("display", 0.0))
                topd = float(getattr(self._last_features, "dob", 0.0)) if self._last_features else 0.0
                if disp > 0.0 and topd > 0.0:
                    self.risk.register_order_post(display_size=disp, top_depth=topd)  # 5秒合計へ加算
                    self.metrics.set_book_impact_5s(self.risk.book_impact_sum_5s())   # Gauge を最新化
            except Exception:
                pass

        # 〔このブロックがすること〕 submitted で order_id→trace_id を登録、cancel で削除する
        try:
            if kind == "submitted":
                oid = str(fields.get("order_id", "") or "")
                tid = fields.get("trace_id")
                if oid and tid:
                    self._order_trace[oid] = str(tid)
            elif kind == "cancel":
                oid = str(fields.get("order_id", "") or "")
                if oid:
                    self._order_trace.pop(oid, None)
        except Exception:
            pass

        # 〔このブロックがすること〕 skip（露出/クールダウン）をメトリクスへ加算し、意思決定ログに残す
        try:
            if kind == "skip":
                reason = str(fields.get("reason", ""))
                side = fields.get("side")
                open_maker = fields.get("open_maker_btc")
                trace = fields.get("trace_id")
                if reason == "exposure":
                    self.metrics.inc_order_skips_exposure(1)
                elif reason == "cooldown":
                    self.metrics.inc_order_skips_cooldown(1)
                self.decisions.log(
                    "skip",
                    reason=reason,
                    side=side,
                    open_maker_btc=open_maker,
                    trace_id=trace,
                )
        except Exception:
            pass

    async def _trigger_killswitch(self, reason: str) -> None:
        """〔このメソッドがすること〕
        Kill‑switch 発火時に「即フラット → メトリクス落とす → 戦略停止」を安全に行います。
        """
        if self._stopping.is_set():
            return
        # 意思決定ログ
        try:
            self.decisions.log("killswitch", reason=str(reason))
        except Exception:
            pass
        # 即フラット（IOC）
        try:
            await self.exe.flatten_ioc()
        except Exception:
            pass
        # Active を 0 に（監視用）
        try:
            self.metrics.set_active(False)
        except Exception:
            pass
        # 戦略停止フラグ
        self._stopping.set()

    def _on_gate_eval(self, g: dict) -> None:
        """〔この関数がすること〕
        SignalDetector から受け取ったゲート評価をメトリクスに反映し、
        位相ゲートは通過しているのに他ゲートで不成立のときだけ decision log に記録します。
        """
        try:
            phase_gate = bool(g.get("phase_gate", False))
            dob_thin = bool(g.get("dob_thin", False))
            spread_ok = bool(g.get("spread_ok", False))
            obi_ok = bool(g.get("obi_ok", False))
            all_pass = phase_gate and dob_thin and spread_ok and obi_ok

            if all_pass:
                self.metrics.inc_gate_all_pass()
                return

            # 個別ミスをカウント
            if not phase_gate:
                self.metrics.inc_gate_phase_miss()
            if not dob_thin:
                self.metrics.inc_gate_dob_miss()
            if not spread_ok:
                self.metrics.inc_gate_spread_miss()
            if not obi_ok:
                self.metrics.inc_gate_obi_miss()

            # 位相ゲートは通っていて他で落ちたときだけ、軽量にログへ（スパム防止）
            if phase_gate and not all_pass:
                missing = []
                if not dob_thin:
                    missing.append("dob")
                if not spread_ok:
                    missing.append("spread")
                if not obi_ok:
                    missing.append("obi")
                self.decisions.log(
                    "gate_fail",
                    missing=missing,
                    phase=float(g.get("phase", 0.0)),
                    spread_ticks=float(g.get("spread_ticks", 0.0)),
                    dob=float(g.get("dob", 0.0)),
                    obi=float(g.get("obi", 0.0)),
                )
        except Exception:
            pass


    async def _fills_loop(self) -> None:
        """〔このメソッドがすること〕
        取引所の fills（約定）ストリームを購読し、1件ごとに：
          - 充足価格と直近 mid から滑り（ticks）を計算しメトリクスへ観測
          - RiskManager へ滑りを登録（1分平均などの判定に利用）
          - ExecutionEngine へ register_fill(side) でクールダウンを発火
          - Decision Log に fill を 1行JSON で記録
        WS アダプタが無い環境では停止イベントまで安全に待機します。
        """
        try:
            from hl_core.api.ws import subscribe_fills  # type: ignore
        except Exception as e:
            logger.warning("fills WS adapter not available: %s; fills_loop idle.", e)
            await self._stopping.wait()
            return

        tick = float(getattr(self.cfg.symbol, "tick_size", 0.5))

        async for ev in subscribe_fills(getattr(self.cfg.symbol, "name", "BTCUSD-PERP")):
            if self._stopping.is_set():
                break

            # 約定の基本情報を安全に取り出す
            try:
                side = str(getattr(ev, "side", "")).upper()
                price = float(getattr(ev, "price", 0.0))
                ts = float(getattr(ev, "t", None) or getattr(ev, "timestamp", None) or time.time())
                oid = str(getattr(ev, "order_id", "") or "")
                trace = self._order_trace.get(oid) if oid else None  # 〔この行がすること〕 約定が紐づく trace_id を対応表から取得（無ければ None）
            except Exception:
                continue

            # 〔このブロックがすること〕 maker の子注文が fill されたら、その order_id で未約定露出を即座に減算する
            if oid:
                try:
                    self.exe.on_child_filled(oid)
                except Exception:
                    logger.debug("on_child_filled failed (ignored)")

            # 参照 mid（最新スナップショットが無ければ fill 価格を使って滑り0扱い）
            snap = self._last_features
            ref_mid = float(getattr(snap, "mid", price)) if snap else float(price)
            slip_ticks = 0.0 if tick <= 0 else abs(price - ref_mid) / tick

            # メトリクス：滑り観測 + 件数カウント
            try:
                self.metrics.observe_slippage(slip_ticks)
                self.metrics.inc_fills(1)
            except Exception:
                logger.debug("metrics.observe_slippage/inc_fills failed (ignored)")

            # リスク：滑りを登録（1分平均の監視などに利用）
            try:
                self.risk.register_fill(fill_price=price, ref_mid=ref_mid, tick_size=tick)
            except Exception:
                logger.debug("risk.register_fill failed (ignored)")

            # クールダウン：充足方向にクールダウンを設定し、Gauge を更新
            try:
                self.exe.register_fill(side)
                period = float(self.rot.current_period() or 1.0)
                self.metrics.set_cooldown(self.exe.cooldown_factor * period)
            except Exception:
                logger.debug("cooldown update after fill failed (ignored)")

            # 意思決定ログ：fill を記録（trace_id は不明なら省略）
            try:
                self.decisions.log(
                    "fill",
                    side=side,
                    price=float(price),
                    ref_mid=float(ref_mid),
                    slip_ticks=float(slip_ticks),
                    order_id=oid or None,
                    trace_id=trace,
                    timestamp=float(ts),
                )
            except Exception:
                logger.debug("decision log (fill) failed (ignored)")

    async def _blocks_loop(self) -> None:
        """〔このメソッドがすること〕
        ブロックWSを購読し、隣接ブロックの間隔（秒）を計算して
        - RiskManager.update_block_interval(interval_s) に渡す（kill‑switch 判定用）
        - Metrics.observe_block_interval_ms(interval_ms) に送る（監視用）
        - Decision Log にも 1 行 JSON を記録する
        WS アダプタが無い環境では停止イベントまで安全に待機します。
        """
        try:
            from hl_core.api.ws import subscribe_blocks  # type: ignore
        except Exception as e:
            logger.warning("blocks WS adapter not available: %s; blocks_loop idle.", e)
            await self._stopping.wait()
            return

        prev_ts: float | None = None

        async for blk in subscribe_blocks(getattr(self.cfg.symbol, "name", "BTCUSD-PERP")):
            if self._stopping.is_set():
                break
            # ブロック時刻（秒）を安全に取得
            ts = float(getattr(blk, "timestamp", None) or getattr(blk, "t", None) or time.time())

            # 直前ブロックがあれば間隔を計算
            if prev_ts is not None:
                interval = ts - prev_ts
                try:
                    self.risk.update_block_interval(interval)
                except Exception:
                    logger.debug("risk.update_block_interval failed (ignored)")
                try:
                    self.metrics.observe_block_interval_ms(interval)
                except Exception:
                    logger.debug("metrics.observe_block_interval_ms failed (ignored)")
                try:
                    self.decisions.log("block_interval", interval_s=float(interval))
                except Exception:
                    logger.debug("decision log (block_interval) failed (ignored)")
                # 〔このブロックがすること〕 ブロック間隔由来の kill‑switch が立ったら即フラット＆停止
                try:
                    adv_now = self.risk.advice()
                    if adv_now.killswitch and not self._stopping.is_set():
                        await self._trigger_killswitch(adv_now.reason)
                        break
                except Exception:
                    logger.debug("killswitch trigger from blocks_loop failed (ignored)")

            prev_ts = ts

    async def _wait_spread_collapse(self, threshold_ticks: float, timeout_s: float, poll_s: float = 0.02) -> bool:
        """〔このメソッドがすること〕
        直近スナップショットの spread_ticks が threshold 以下になるまで待ちます。
        timeout_s を過ぎたら False。停止フラグが立っても False を返します。
        """

        t0 = time.time()
        while (time.time() - t0) < float(timeout_s):
            if self._stopping.is_set():
                return False
            snap = self._last_features
            if snap is not None and float(getattr(snap, "spread_ticks", 1e9)) <= float(threshold_ticks):
                return True
            try:
                await asyncio.sleep(max(0.0, float(poll_s)))
            except Exception:
                break
        return False

    async def _exec_loop(self) -> None:
        """〔このメソッドがすること〕
        シグナルを受けてリスク判定→post-only Iceberg の発注→TTL待ち→IOC解消を実行します。
        """
        while not self._stopping.is_set():
            try:
                sig = await self.q_signals.get()

                # リスク助言（キル/一時停止/サイズ倍率/成行禁止など）
                adv = self.risk.advice()
                # 〔このブロックがすること〕 kill‑switch なら即停止、そうでなければ一時停止を尊重
                if adv.killswitch:
                    await self._trigger_killswitch(adv.reason)
                    return
                if self.risk.should_pause():
                    self.decisions.log("risk_pause", killswitch=False, paused_until=adv.paused_until, reason=str(adv.reason))
                    await asyncio.sleep(0.1)
                    continue

                # 〔この行がすること〕 口座残高の0.2–0.5%を基準に、リスク倍率を反映して1クリップのBTCサイズを決める
                self.exe.trace_id = getattr(sig, "trace_id", None)  # 〔この行がすること〕 発注エンジンへ相関IDを注入し、以降のイベントに載せる
                clip = self.sizer.next_size(mid=sig.mid, risk_mult=adv.size_multiplier)
                self.decisions.log(
                    "order_intent",
                    mid=float(sig.mid),
                    clip=float(clip),
                    deepen=bool(adv.deepen_post_only),
                    trace_id=getattr(sig, "trace_id", None),
                )  # 〔この行がすること〕 置くサイズと深さの意図を記録

                # 〔この行がすること〕 Time-Stop を開始（ms後に IOC で強制クローズ）します
                time_stop_ms = int(getattr(self.cfg.risk, "time_stop_ms", 1200))
                ts_task = asyncio.create_task(self.exe.time_stop_after(time_stop_ms), name="time_stop")

                # 〔このブロックがすること〕
                # 逆指値（Reduce‑Only の STOP）を両方向に“仮置き”し、Time‑Stop 終了後に自動で片付けます。
                stop_ticks = float(getattr(self.cfg.risk, "stop_ticks", 3))
                stop_ids: list[str] = []
                sid_buy = await self.exe.place_reverse_stop("BUY", sig.mid, stop_ticks)
                sid_sell = await self.exe.place_reverse_stop("SELL", sig.mid, stop_ticks)
                for _sid in (sid_buy, sid_sell):
                    if _sid:
                        stop_ids.append(_sid)

                async def _cleanup_stops_after_ts() -> None:
                    """〔この内部関数がすること〕
                    Time‑Stop タイマーが発火してクローズした後、残存 STOP を安全にキャンセルします。
                    """

                    with contextlib.suppress(asyncio.CancelledError):
                        await ts_task
                        for _sid in stop_ids:
                            await self.exe.cancel_order_safely(_sid)

                stops_cleanup_task = asyncio.create_task(_cleanup_stops_after_ts(), name="stops_cleanup")

                # 〔このブロックがすること〕 発注直前の最終鮮度チェック（安全弁）
                snap = self._last_features
                if snap is None:
                    self.metrics.inc_staleness_skips()
                    self.decisions.log("stale_exec_skip", reason="no_feature")
                    continue
                snap_ts = float(snap.t)
                enforce_guard = snap_ts > 1e6  # 〔この条件がすること〕 epoch 秒である場合のみ鮮度ガードを有効化します
                staleness_ms = 0.0
                if enforce_guard:
                    staleness_ms = max(0.0, (time.time() - snap_ts) * 1000.0)
                self.metrics.set_data_staleness_ms(staleness_ms)
                max_stale = float(getattr(self.cfg.latency, "max_staleness_ms", 300))
                if enforce_guard and staleness_ms > max_stale:
                    self.metrics.inc_staleness_skips()
                    self.decisions.log("stale_exec_skip", staleness_ms=float(staleness_ms), max_allowed_ms=float(max_stale))
                    continue

                order_ids = await self.exe.place_two_sided(sig.mid, clip, deepen=adv.deepen_post_only)  # 〔この行がすること〕 リスク助言に応じて「深置き」を切り替える
                self.decisions.log("order_submitted", count=int(len(order_ids)))  # 〔この行がすること〕 実際に何件出したかを記録
                self.metrics.inc_orders_submitted(len(order_ids))  # 〔この行がすること〕 提示した注文（maker）の件数を加算

                async def _cancel_stops_and_timers() -> None:
                    for _sid in stop_ids:
                        await self.exe.cancel_order_safely(_sid)
                    if not ts_task.done():
                        ts_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await ts_task
                    if not stops_cleanup_task.done():
                        stops_cleanup_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await stops_cleanup_task

                # 〔このブロックがすること〕
                # 「TTL経過」 vs 「スプレッド≤1tick縮小」の先着で処理を分岐します。

                ttl_s = float(getattr(self.cfg.exec, "order_ttl_ms", 1000)) / 1000.0  # 〔この行がすること〕 設定の TTL(ms) を秒へ直して以降の待機/解消に共通利用する

                wait_start = time.time()
                # 早期エグジット候補：スプレッドが 1 tick に縮小したら即クローズ
                # 〔この行がすること〕 しきい値を設定から受け取り、縮小判定に使う
                collapsed = await self._wait_spread_collapse(
                    threshold_ticks=float(getattr(self.cfg.exec, "spread_collapse_ticks", 1.0)),
                    timeout_s=ttl_s,
                    poll_s=0.02,
                )
                # 〔このブロックがすること〕 forbid_market の場合は早期IOCをスキップする旨を先に記録（TTL へフォールバック）
                if collapsed and adv.forbid_market:
                    self.decisions.log("exit_policy", policy="forbid_market_skip_ioc", trace_id=getattr(sig, "trace_id", None))
                elapsed = time.time() - wait_start
                remaining_ttl = max(0.0, ttl_s - elapsed)

                if adv.forbid_market:
                    self.decisions.log("exit_policy", policy="forbid_market")  # 〔この行がすること〕 早期IOCを行わない方針であることを記録
                    # 成行は禁止 → 通常通り TTL まで待ってキャンセル（Time‑Stopは別途走る）

                    await self.exe.wait_fill_or_ttl(order_ids, timeout_s=ttl_s)  # 〔この行がすること〕 ハードコードの 1.0 秒を廃し、設定由来の TTL 秒を使う


                    self.decisions.log("exit", reason="ttl", trace_id=getattr(sig, "trace_id", None))  # 〔この行がすること〕 TTL 到達で通常解消したことを記録

                else:
                    if collapsed and not adv.forbid_market:  # 〔この行がすること〕 forbid_market=True のときは早期IOCを行わず、TTL 処理へ回す
                        self.decisions.log("exit", reason="spread_collapse", trace_id=getattr(sig, "trace_id", None))  # 〔この行がすること〕 スプレッド縮小で早期IOCしたことを記録
                        # 〔このブロックがすること〕 早期IOCでクローズしたので、保護用STOPを取り消し、Time‑Stopを中断する
                        try:
                            for _sid in stop_ids:
                                await self.exe.cancel_order_safely(_sid)  # STOP注文の取消（reduce-only）
                        except Exception:
                            pass
                        try:
                            ts_task.cancel()  # Time‑Stopタスクを中断
                        except Exception:
                            pass
                        # 先に maker を素早くキャンセルしてから IOC で解消
                        await self.exe.wait_fill_or_ttl(order_ids, timeout_s=0.0)

                        await self.exe.flatten_ioc()
                        await _cancel_stops_and_timers()
                    else:
                        self.decisions.log("exit", reason="ttl", trace_id=getattr(sig, "trace_id", None))  # 〔この行がすること〕 TTL 到達で通常解消したことを記録
                        # 〔このブロックがすること〕 TTL到達でクローズしたので、保護用STOPを取り消し、Time‑Stopを中断する
                        try:
                            for _sid in stop_ids:
                                await self.exe.cancel_order_safely(_sid)  # STOP注文の取消（reduce-only）
                        except Exception:
                            pass
                        try:
                            ts_task.cancel()  # Time‑Stopタスクを中断
                        except Exception:
                            pass
                        # 縮小しなかった → TTL まで待って通常解消
                        await self.exe.wait_fill_or_ttl(order_ids, timeout_s=remaining_ttl)

                        await self.exe.flatten_ioc()
                        await _cancel_stops_and_timers()

                cd = self.exe.cooldown_factor * (self.rot.current_period() or 1.0)
                self.metrics.set_cooldown(cd)                     # 〔この行がすること〕 現在のクールダウン秒数をGaugeへ
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover
                logger.exception("exec_loop error: %s", e)

    async def shutdown(self) -> None:
        """〔このメソッドがすること〕
        全タスクを安全に停止し、フラット化（必要なら）して終了します。
        """
        if self._stopping.is_set():
            return
        logger.info("VRLG shutting down…")
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.exe:
            await self.exe.flatten_ioc()
        logger.info("VRLG stopped.")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """〔この関数がすること〕
    CLI 引数を解釈します。（--config, --paper/--live, --log-level）
    """
    p = argparse.ArgumentParser(prog="vrlg", description="VRLG high-frequency bot")
    p.add_argument("--config", required=True, help="path to VRLG config (TOML/YAML)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_true", help="paper trading mode")
    g.add_argument("--live", action="store_true", help="live trading mode")
    p.add_argument("--log-level", default="INFO", help="logging level")
    p.add_argument("--prom-port", type=int, default=None, help="Prometheus metrics port (optional)")  # 〔この行がすること〕 /metrics を公開するポート番号の受け取り
    p.add_argument("--decisions-file", default=None, help="path to JSONL file for decision logs (optional)")  # 〔この行がすること〕 意思決定ログの保存先を受け取る
    return p.parse_args(argv)


async def _run(argv: list[str]) -> int:
    """〔この関数がすること〕
    uvloop を可能なら有効化し、VRLGStrategy を起動してシグナルで停止します。
    """
    args = parse_args(argv)
    # 〔この行がすること〕 CLI の --log-level を VRLG ロガーへ反映する
    try:
        logger.setLevel(str(args.log_level).upper())
    except Exception:
        pass
    if uvloop is not None:
        uvloop.install()

    strategy = VRLGStrategy(config_path=args.config, paper=not args.live, prom_port=args.prom_port, decisions_file=args.decisions_file)  # 〔この行がすること〕 CLI からロガーへ出力先を渡す

    await strategy.start()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _handle_sig(*_: object) -> None:
        stop.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _handle_sig)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass

    await stop.wait()
    await strategy.shutdown()
    return 0


def main() -> None:
    """〔この関数がすること〕
    エントリポイント。例外を整形して終了コードを返します。
    """
    try:
        exit_code = asyncio.run(_run(sys.argv[1:]))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()


