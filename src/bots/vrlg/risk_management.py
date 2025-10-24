# 〔このモジュールがすること〕
# VRLG のルールベース・リスク管理を提供します。
# - 助言: advice() → killswitch / size_multiplier / forbid_market / deepen_post_only / reason
# - 更新: update_block_interval(), register_order_post(), register_fill()
# - 監視: should_pause(), book_impact_sum_5s()
# 仕様（既定値）は設計書に準拠:
#   ・ブロック間隔（移動中央値）> 4s → kill-switch（即フラット&停止）
#   ・板消費率（自分の表示量/TopDepth の5秒合計）> 2% → サイズ50%減
#   ・滑り（1分平均）> 1tick → 成行禁止 + post-only深置き
#   ・連続損切り 3回/10m → 一時停止（10分）

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

import logging

logger = logging.getLogger("bots.vrlg.risk")


@dataclass
class Advice:
    """〔このデータクラスがすること〕
    発注直前に Strategy へ返す助言パケットです。
    """

    killswitch: bool
    forbid_market: bool
    deepen_post_only: bool
    size_multiplier: float
    paused_until: float
    reason: str


class RiskManager:
    """〔このクラスがすること〕
    ルールベースのリスク管理を行い、発注時の挙動（サイズ/成行/置き方）を調整します。
    """

    def __init__(self, cfg) -> None:
        """〔このメソッドがすること〕 設定値と内部バッファを初期化します。"""
        rk = getattr(cfg, "risk", {})
        # しきい値（TOML 未定義でも既定値で動作）
        self.max_slippage_ticks: float = float(getattr(rk, "max_slippage_ticks", 1.0))
        self.max_book_impact: float = float(getattr(rk, "max_book_impact", 0.02))
        self.block_interval_stop_s: float = float(getattr(rk, "block_interval_stop_s", 4.0))  # 設計既定: 4s

        # 窓長
        self._impact_window_s: float = 5.0
        self._slip_window_s: float = 60.0
        self._block_median_len: int = 50  # 移動中央値用の保有数

        # 内部状態
        self._impact_events: Deque[Tuple[float, float]] = deque()  # (ts, display/TopDepth)
        self._slip_events: Deque[Tuple[float, float]] = deque()  # (ts, slip_ticks)
        self._block_intervals: Deque[float] = deque(maxlen=self._block_median_len)
        self._killswitch: bool = False
        self._pause_until: float = 0.0
        self._stopouts: Deque[float] = deque()  # 損切りの発生時刻列（register_stopout で使用）

    # ─────────────── 更新API ───────────────

    def update_block_interval(self, interval_s: float) -> None:
        """〔この関数がすること〕
        ブロック間隔（秒）を記録し、移動中央値がしきい値を超えたら kill‑switch を有効化します。
        """
        try:
            self._block_intervals.append(max(0.0, float(interval_s)))
            if len(self._block_intervals) >= max(5, int(self._block_median_len * 0.6)):
                med = statistics.median(self._block_intervals)
                if med > self.block_interval_stop_s:
                    if not self._killswitch:
                        logger.warning(
                            "kill-switch: block interval median %.3fs > %.3fs",
                            med,
                            self.block_interval_stop_s,
                        )
                    self._killswitch = True
        except Exception:
            # 例外は戦略停止の妨げにならないよう握りつぶす
            self._killswitch = self._killswitch or False

    def register_order_post(self, display_size: float, top_depth: float) -> None:
        """〔この関数がすること〕
        post-only 指値の「板消費率（表示量/TopDepth）」をイベントとして記録します。
        集計は 5 秒の可変窓で行います。
        """
        if display_size <= 0 or top_depth <= 0:
            return
        frac = float(display_size) / float(top_depth)
        now = time.time()
        self._impact_events.append((now, max(0.0, frac)))
        self._trim_impacts(now)

    def register_fill(self, fill_price: float, ref_mid: float, tick_size: float) -> None:
        """〔この関数がすること〕
        約定の滑り（ticks）を計算して 1 分窓へ記録します。
        """
        if tick_size <= 0:
            return
        slip = abs(float(fill_price) - float(ref_mid)) / float(tick_size)
        now = time.time()
        self._slip_events.append((now, float(slip)))
        self._trim_slips(now)

    def register_stopout(self) -> None:
        """〔この関数がすること〕
        損切り（STOP 約定）を1件として記録します。10分窓で3件に達したら一時停止（10分）。
        ※ 実際の STOP 約定検知から呼び出してください（将来の配線用）。
        """
        now = time.time()
        self._stopouts.append(now)
        # 10分窓にトリム
        ten_min_ago = now - 600.0
        while self._stopouts and self._stopouts[0] < ten_min_ago:
            self._stopouts.popleft()
        if len(self._stopouts) >= 3:
            self.pause_for(600.0)
            logger.warning("temporary pause: 3 stopouts within 10m → paused for 10m")

    # ─────────────── 参照API ───────────────

    def advice(self) -> Advice:
        """〔この関数がすること〕
        現在までの観測から「発注時の助言」を返します。
        - killswitch: True なら戦略は即フラット＆停止
        - size_multiplier: 板消費率 > しきい値なら 0.5、それ以外は 1.0
        - forbid_market, deepen_post_only: 1分平均滑り > しきい値なら True
        - reason: 判定根拠の要約（監視・ログ用）
        """
        now = time.time()

        # 1) 板消費率（5秒合計）
        impact_sum = self.book_impact_sum_5s(now)
        size_mult = 0.5 if impact_sum > self.max_book_impact else 1.0

        # 2) 滑り（1分平均）
        slip_avg = self._slip_avg(now)
        slip_bad = (slip_avg is not None) and (slip_avg > self.max_slippage_ticks)
        forbid_mkt = bool(slip_bad)
        deepen = bool(slip_bad)

        # 3) キル・一時停止
        ks = bool(self._killswitch)
        pause_until = float(self._pause_until)
        paused = now < pause_until

        # 4) 理由メッセージ
        parts = []
        if ks:
            parts.append("kill: block_interval_median>threshold")
        if size_mult < 1.0:
            parts.append(f"size↓ impact5s={impact_sum:.3f}>{self.max_book_impact:.3f}")
        if slip_bad:
            parts.append(f"slip_avg1m={slip_avg:.2f}>max={self.max_slippage_ticks:.2f}")
        if paused:
            remain = max(0.0, pause_until - now)
            parts.append(f"paused {remain:.0f}s")
        reason = "; ".join(parts) if parts else "ok"

        return Advice(
            killswitch=ks,
            forbid_market=forbid_mkt,
            deepen_post_only=deepen,
            size_multiplier=size_mult,
            paused_until=pause_until,
            reason=reason,
        )

    def should_pause(self) -> bool:
        """〔この関数がすること〕 一時停止中かどうかを返します（kill-switch とは独立）。"""
        return time.time() < float(self._pause_until)

    def pause_for(self, seconds: float) -> None:
        """〔この関数がすること〕 指定秒数だけ一時停止にします。"""
        self._pause_until = max(self._pause_until, time.time() + max(0.0, float(seconds)))

    def book_impact_sum_5s(self, now: Optional[float] = None) -> float:
        """〔この関数がすること〕
        直近5秒の板消費率（display/TopDepth）の合計を返します（Metrics 更新にも使用）。
        """
        if now is None:
            now = time.time()
        self._trim_impacts(now)
        return float(sum(x for _, x in self._impact_events))

    # ─────────────── 内部ユーティリティ ───────────────

    def _trim_impacts(self, now: Optional[float] = None) -> None:
        """〔この関数がすること〕 板消費率イベントを 5 秒窓にトリムします。"""
        if now is None:
            now = time.time()
        limit = float(now) - float(self._impact_window_s)
        while self._impact_events and self._impact_events[0][0] < limit:
            self._impact_events.popleft()

    def _trim_slips(self, now: Optional[float] = None) -> None:
        """〔この関数がすること〕 滑りイベントを 60 秒窓にトリムします。"""
        if now is None:
            now = time.time()
        limit = float(now) - float(self._slip_window_s)
        while self._slip_events and self._slip_events[0][0] < limit:
            self._slip_events.popleft()

    def _slip_avg(self, now: Optional[float] = None) -> Optional[float]:
        """〔この関数がすること〕 1 分窓の平均滑り（ticks）を返します（データ無しなら None）。"""
        if now is None:
            now = time.time()
        self._trim_slips(now)
        if not self._slip_events:
            return None
        s = sum(x for _, x in self._slip_events)
        return float(s / len(self._slip_events))
