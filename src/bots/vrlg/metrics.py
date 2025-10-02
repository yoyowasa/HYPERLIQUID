# 〔このモジュールがすること〕
# VRLG のメトリクス（Prometheus）を一元管理します。
# - prometheus_client が無い環境では No-op で安全に動作します。
# - HTTP エクスポータ（/metrics）は __init__(prom_port) で指定があれば起動します。

from __future__ import annotations

from typing import Optional

from hl_core.utils.logger import get_logger

logger = get_logger("VRLG.metrics")

# ───────────────── No-op フォールバック ─────────────────
try:
    from prometheus_client import Gauge, Counter, Histogram, start_http_server  # type: ignore
except Exception:  # ランタイムに prometheus_client が無ければ安全に No-op
    class _Noop:
        def labels(self, *_, **__):
            return self

        def set(self, *_, **__):
            return None

        def inc(self, *_, **__):
            return None

        def observe(self, *_, **__):
            return None

    Gauge = Counter = Histogram = lambda *_, **__: _Noop()  # type: ignore

    def start_http_server(*_, **__):  # type: ignore
        logger.warning("prometheus_client not found; metrics exporter disabled")


# ───────────────── Metrics 実装 ─────────────────


class Metrics:
    """〔このクラスがすること〕
    VRLG が使用するメトリクス（Gauge/Counter/Histogram）を提供し、
    必要に応じて Prometheus HTTP エクスポータを起動します。
    """

    def __init__(self, prom_port: Optional[int] = None, *, port: Optional[int] = None) -> None:
        """〔このメソッドがすること〕
        各メトリクスの器を作成し、prom_port が指定されたときだけエクスポータを起動します。
        """
        if prom_port is None and port is not None:
            prom_port = port
        # 基本ステータス
        self.period_s = Gauge("vrlg_period_s", "Estimated rotation period R* (seconds).")
        self.active_flag = Gauge("vrlg_is_active", "Rotation gate active (1) or paused (0).")
        self.cooldown_s = Gauge("vrlg_cooldown_s", "Current cooldown window (s).")
        self.signal_total = Counter("vrlg_signals", "Number of signals emitted.")

        # 実行系
        self.slippage_ticks = Histogram("vrlg_slippage_ticks", "Per-fill slippage (ticks).")
        self.fills = Counter("vrlg_fills", "Number of fills observed.")
        self.signal_count = Counter(
            "vrlg_signal_count", "Number of actionable signals (rotation active)."
        )  # 〔この行がすること〕 実行対象のシグナル件数を数える
        self.orders_rejected = Counter("vrlg_orders_rejected", "Number of orders rejected by venue.")
        self.orders_canceled = Counter(
            "vrlg_orders_canceled", "Number of orders canceled (TTL/explicit)."
        )
        self.orders_submitted = Counter("vrlg_orders_submitted", "Number of maker orders submitted.")

        # 市場構造/鮮度
        self.block_interval_ms_hist = Histogram("vrlg_block_interval_ms", "Block interval (ms).")
        self.block_interval_last_ms = Gauge(
            "vrlg_block_interval_last_ms", "Last observed block interval (ms)."
        )
        self.data_staleness_ms = Gauge("vrlg_data_staleness_ms", "Age of the latest feature snapshot (ms).")
        self.staleness_skips = Counter("vrlg_staleness_skips", "Skips due to stale features.")
        self.spread_ticks_hist = Histogram("vrlg_spread_ticks", "Observed spread in ticks (feature snapshots).")

        # リスク・露出
        self.book_impact_5s = Gauge("vrlg_book_impact_5s", "5s sum of book impact (display/TopDepth).")
        self.open_maker_btc = Gauge("vrlg_open_maker_btc", "Open maker exposure (BTC).")

        # ゲート内訳
        self.gate_phase_miss = Counter(
            "vrlg_gate_phase_miss", "Gate miss count: phase window not satisfied."
        )
        self.gate_dob_miss = Counter("vrlg_gate_dob_miss", "Gate miss count: DoB not thin enough.")
        self.gate_spread_miss = Counter(
            "vrlg_gate_spread_miss", "Gate miss count: spread below threshold."
        )
        self.gate_obi_miss = Counter("vrlg_gate_obi_miss", "Gate miss count: |OBI| above limit.")
        self.gate_all_pass = Counter("vrlg_gate_all_pass", "All gates passed (pre-signal).")

        # Rotation 検出の品質
        self.rotation_score = Gauge("vrlg_rotation_score", "Autocorrelation score used for R* selection.")
        self.rotation_boundary_samples = Gauge(
            "vrlg_rotation_boundary_samples", "Boundary sample count."
        )
        self.rotation_p_dob = Gauge(
            "vrlg_rotation_p_dob", "p-value for DoB thinness at boundary (one-sided)."
        )
        self.rotation_p_spr = Gauge(
            "vrlg_rotation_p_spr", "p-value for spread wideness at boundary (one-sided)."
        )

        # エクスポータ起動（任意）
        if prom_port is not None:
            try:
                start_http_server(int(prom_port))
                logger.info("Prometheus exporter started on :%s", prom_port)
            except Exception as e:
                logger.warning("failed to start Prometheus exporter: %s", e)

    # ─────────── Setter / Observer 群（ここから下は呼び先から使用） ───────────

    def set_period(self, seconds: float) -> None:
        """〔この関数がすること〕 推定周期 R*（秒）を Gauge に反映します。"""
        try:
            self.period_s.set(max(0.0, float(seconds)))
        except Exception:
            pass

    def set_active(self, is_active: bool) -> None:
        """〔この関数がすること〕 Rotation ゲートの有効/無効を 1/0 で設定します。"""
        try:
            self.active_flag.set(1.0 if bool(is_active) else 0.0)
        except Exception:
            pass

    def set_cooldown(self, seconds: float) -> None:
        """〔この関数がすること〕 現在のクールダウン窓（秒）を Gauge に設定します。"""
        try:
            self.cooldown_s.set(max(0.0, float(seconds)))
        except Exception:
            pass

    def observe_slippage(self, slip_ticks: float) -> None:
        """〔この関数がすること〕 充足スリッページ（ticks）を Histogram に観測します。"""
        try:
            self.slippage_ticks.observe(max(0.0, float(slip_ticks)))
        except Exception:
            pass

    def inc_fills(self, n: int = 1) -> None:
        """〔この関数がすること〕 約定件数カウンタを +n します。"""
        try:
            self.fills.inc(int(n))
        except Exception:
            pass

    def inc_signals(self, n: int = 1) -> None:
        """〔この関数がすること〕 実行対象のシグナル件数を +n します（Rotation が Active のときに限る想定）。"""
        try:
            self.signal_count.inc(int(n))
        except Exception:
            pass

    def observe_block_interval_ms(self, ms: float) -> None:
        """〔この関数がすること〕 ブロック間隔（ms）を Histogram と Gauge に反映します。"""
        try:
            v = max(0.0, float(ms))
            self.block_interval_ms_hist.observe(v)
            self.block_interval_last_ms.set(v)
        except Exception:
            pass

    def set_book_impact_5s(self, value: float) -> None:
        """〔この関数がすること〕 直近5秒の板消費率合計を Gauge に設定します。"""
        try:
            self.book_impact_5s.set(max(0.0, float(value)))
        except Exception:
            pass

    def set_open_maker_btc(self, value: float) -> None:
        """〔この関数がすること〕 未約定メーカー露出（BTC）を Gauge に設定します。"""
        try:
            self.open_maker_btc.set(max(0.0, float(value)))
        except Exception:
            pass

    def inc_orders_rejected(self, n: int = 1) -> None:
        """〔この関数がすること〕 拒否件数を +n します。"""
        try:
            self.orders_rejected.inc(int(n))
        except Exception:
            pass

    def inc_orders_canceled(self, n: int = 1) -> None:
        """〔この関数がすること〕 キャンセル件数を +n します。"""
        try:
            self.orders_canceled.inc(int(n))
        except Exception:
            pass

    def inc_orders_submitted(self, n: int = 1) -> None:
        """〔この関数がすること〕 提示した注文件数を +n します。"""
        try:
            self.orders_submitted.inc(int(n))
        except Exception:
            pass

    # －－－ ゲート内訳 －－－
    def inc_gate_phase_miss(self) -> None:
        """〔この関数がすること〕 位相ゲート不成立を +1 カウントします。"""
        try:
            self.gate_phase_miss.inc()
        except Exception:
            pass

    def inc_gate_dob_miss(self) -> None:
        """〔この関数がすること〕 DoB 薄さ不成立を +1 カウントします。"""
        try:
            self.gate_dob_miss.inc()
        except Exception:
            pass

    def inc_gate_spread_miss(self) -> None:
        """〔この関数がすること〕 スプレッド閾未満を +1 カウントします。"""
        try:
            self.gate_spread_miss.inc()
        except Exception:
            pass

    def inc_gate_obi_miss(self) -> None:
        """〔この関数がすること〕 OBI 上限超過を +1 カウントします。"""
        try:
            self.gate_obi_miss.inc()
        except Exception:
            pass

    def inc_gate_all_pass(self) -> None:
        """〔この関数がすること〕 4 条件すべて通過を +1 カウントします。"""
        try:
            self.gate_all_pass.inc()
        except Exception:
            pass

    # －－－ 鮮度 －－－
    def set_data_staleness_ms(self, value_ms: float) -> None:
        """〔この関数がすること〕 特徴量の鮮度（ms）を Gauge に設定します。"""
        try:
            self.data_staleness_ms.set(max(0.0, float(value_ms)))
        except Exception:
            pass

    def inc_staleness_skips(self) -> None:
        """〔この関数がすること〕 鮮度不足スキップを +1 します。"""
        try:
            self.staleness_skips.inc()
        except Exception:
            pass

    def observe_spread(self, spread_ticks: float) -> None:
        """〔この関数がすること〕 観測スプレッド（ticks）を Histogram に登録します。"""
        try:
            self.spread_ticks_hist.observe(max(0.0, float(spread_ticks)))
        except Exception:
            pass

    # －－－ Rotation 品質 －－－
    def set_rotation_quality(
        self, score: float, n_boundary: int, p_dob: Optional[float], p_spr: Optional[float]
    ) -> None:
        """〔この関数がすること〕 RotationDetector の品質（score/p値/サンプル数）を Gauge に反映します。"""
        try:
            self.rotation_score.set(float(score))
            self.rotation_boundary_samples.set(max(0.0, float(n_boundary)))
            if p_dob is not None:
                self.rotation_p_dob.set(float(p_dob))
            if p_spr is not None:
                self.rotation_p_spr.set(float(p_spr))
        except Exception:
            pass

    def inc_signal(self) -> None:
        """〔この関数がすること〕 シグナル発火回数を +1 します。"""
        try:
            self.signal_total.inc()
        except Exception:
            pass
