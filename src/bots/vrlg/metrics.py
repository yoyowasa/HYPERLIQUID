# 〔このモジュールがすること〕
# VRLG 戦略の実行状況を Prometheus 形式で公開（または未導入環境では安全に無視）します。
# - Counter/Gauge/Histogram の定義
# - start_http_server による /metrics 公開（port 指定時）
# - Strategy/Execution/Risk から呼びやすい薄いラッパ（inc/set/observe）

from __future__ import annotations

from typing import Optional

from hl_core.utils.logger import get_logger

logger = get_logger("VRLG.metrics")

# ─────────────── Prometheus クライアントの安全な読み込み（No-op フォールバック） ───────────────
try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server  # type: ignore
except Exception:  # pragma: no cover

    class _Noop:
        """〔このクラスがすること〕 prometheus_client が無いときのダミー実装です。"""
        def inc(self, *_args, **_kwargs) -> None: ...
        def set(self, *_args, **_kwargs) -> None: ...
        def observe(self, *_args, **_kwargs) -> None: ...

    def Counter(*_args, **_kwargs):  # type: ignore
        return _Noop()

    def Gauge(*_args, **_kwargs):  # type: ignore
        return _Noop()

    def Histogram(*_args, **_kwargs):  # type: ignore
        return _Noop()

    def start_http_server(*_args, **_kwargs):  # type: ignore
        logger.info("prometheus_client not installed; metrics are no-op.")
        return None


class Metrics:
    """〔このクラスがすること〕
    VRLG で使うメトリクス群の生成と、簡単な操作メソッド（inc/set/observe）を提供します。
    port を与えると start_http_server を起動し /metrics を公開します。
    """

    def __init__(self, port: Optional[int] = None) -> None:
        """〔このメソッドがすること〕
        メトリクスを定義し、port が与えられたら HTTP Exporter を起動します。
        """
        if port is not None:
            try:
                start_http_server(int(port))
                logger.info("Prometheus exporter started on :%s", port)
            except Exception as e:  # pragma: no cover
                logger.warning("failed to start metrics server: %s", e)

        # ── Counters（累積カウント）
        self.signal_count = Counter("vrlg_signal_count", "Number of signals emitted.")
        self.orders_submitted = Counter("vrlg_orders_submitted", "Orders submitted (maker).")
        self.orders_canceled = Counter("vrlg_orders_canceled", "Orders canceled (TTL/flat).")
        self.orders_rejected = Counter("vrlg_orders_rejected", "Order rejections.")
        self.fills = Counter("vrlg_fills", "Number of fills (clips).")

        # ── Gauges（現在値）
        self.current_period_s = Gauge("vrlg_current_period_s", "Estimated rotation period R* (s).")
        self.active_flag = Gauge("vrlg_is_active", "Rotation gate active (1) or paused (0).")
        self.book_impact_5s = Gauge("vrlg_book_impact_5s", "Sum of display/TopDepth over 5s.")
        self.cooldown_s = Gauge("vrlg_cooldown_s", "Current cooldown window (s).")
        self.open_maker_btc = Gauge("vrlg_open_maker_btc", "Open maker exposure (BTC).")  # 〔この行がすること〕 未約定メーカー合計BTCを可視化

        # ── Histograms（分布）
        self.spread_ticks = Histogram(
            "vrlg_spread_ticks", "Top-of-book spread in ticks.",
            buckets=(0.5, 1, 2, 3, 4, 6, 8, 12)
        )
        self.block_interval_ms = Histogram(
            "vrlg_block_interval_ms", "Observed block interval (ms).",
            buckets=(250, 500, 1000, 1500, 2000, 3000, 4000, 6000)
        )
        self.slippage_ticks = Histogram(
            "vrlg_slippage_ticks", "Per-fill slippage (ticks).",
            buckets=(0.0, 0.25, 0.5, 0.75, 1, 1.5, 2, 3)
        )
        self.holding_ms = Histogram(
            "vrlg_holding_ms", "Holding time per trade (ms).",
            buckets=(50, 100, 250, 500, 800, 1200, 2000, 3000)
        )
        self.pnl_bps = Histogram(
            "vrlg_pnl_per_trade_bps", "PnL per trade (bps, net).",
            buckets=(-10, -5, -2, 0, 2, 4, 6, 10, 15, 20)
        )

    # ─────────────── ここから薄いラッパ（関数ごとの役割をコメントで明記） ───────────────

    def set_period(self, period_s: float) -> None:
        """〔この関数がすること〕 推定された R*（秒）を Gauge に設定します。"""
        try:
            self.current_period_s.set(max(0.0, float(period_s)))
        except Exception:
            pass

    def set_active(self, is_active: bool) -> None:
        """〔この関数がすること〕 戦略が稼働可能(1)か観察モード(0)かを設定します。"""
        try:
            self.active_flag.set(1.0 if is_active else 0.0)
        except Exception:
            pass

    def observe_spread(self, spread_ticks: float) -> None:
        """〔この関数がすること〕 観測したスプレッド（ticks）をヒストグラムに記録します。"""
        try:
            self.spread_ticks.observe(float(spread_ticks))
        except Exception:
            pass

    def observe_block_interval_ms(self, interval_s: float) -> None:
        """〔この関数がすること〕 ブロック間隔（秒）をミリ秒に換算して記録します。"""
        try:
            self.block_interval_ms.observe(max(0.0, float(interval_s) * 1000.0))
        except Exception:
            pass

    def inc_signal(self) -> None:
        """〔この関数がすること〕 シグナル発火カウンタを +1 します。"""
        try:
            self.signal_count.inc()
        except Exception:
            pass

    def inc_orders_submitted(self, n: int = 1) -> None:
        """〔この関数がすること〕 発注件数（maker）をカウントアップします。"""
        try:
            for _ in range(max(0, int(n))):
                self.orders_submitted.inc()
        except Exception:
            pass

    def inc_orders_canceled(self, n: int = 1) -> None:
        """〔この関数がすること〕 キャンセル件数（TTL/手動）をカウントアップします。"""
        try:
            for _ in range(max(0, int(n))):
                self.orders_canceled.inc()
        except Exception:
            pass

    def inc_orders_rejected(self, n: int = 1) -> None:
        """〔この関数がすること〕 取引所からの拒否件数をカウントアップします。"""
        try:
            for _ in range(max(0, int(n))):
                self.orders_rejected.inc()
        except Exception:
            pass

    def inc_fills(self, n: int = 1) -> None:
        """〔この関数がすること〕 約定回数（クリップ）をカウントアップします。"""
        try:
            for _ in range(max(0, int(n))):
                self.fills.inc()
        except Exception:
            pass

    def observe_slippage(self, slip_ticks: float) -> None:
        """〔この関数がすること〕 約定ごとの滑り（ticks）を記録します。"""
        try:
            self.slippage_ticks.observe(float(slip_ticks))
        except Exception:
            pass

    def observe_holding_ms(self, holding_ms: float) -> None:
        """〔この関数がすること〕 約定からクローズまでの保有時間（ms）を記録します。"""
        try:
            self.holding_ms.observe(max(0.0, float(holding_ms)))
        except Exception:
            pass

    def observe_pnl_bps(self, pnl_bps: float) -> None:
        """〔この関数がすること〕 取引ごとの損益（bps, 手数料込み）を記録します。"""
        try:
            self.pnl_bps.observe(float(pnl_bps))
        except Exception:
            pass

    def set_book_impact_5s(self, impact_fraction: float) -> None:
        """〔この関数がすること〕 直近5秒の板消費率合計（TopDepth比）を Gauge に設定します。"""
        try:
            self.book_impact_5s.set(max(0.0, float(impact_fraction)))
        except Exception:
            pass

    def set_cooldown(self, seconds: float) -> None:
        """〔この関数がすること〕 現在のクールダウン秒数を Gauge に設定します。"""
        try:
            self.cooldown_s.set(max(0.0, float(seconds)))
        except Exception:
            pass

    def set_open_maker_btc(self, value: float) -> None:
        """〔この関数がすること〕 未約定メーカー合計BTCを Gauge に設定します。"""
        try:
            self.open_maker_btc.set(max(0.0, float(value)))
        except Exception:
            pass
