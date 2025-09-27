# 〔このモジュールがすること〕
# VRLG のルールベースなリスク管理を担当します。
# 監視: ブロック間隔の悪化、板消費率、滑り、連続損切り、ポジション相関（VaR 対比）
# 出力: キルスイッチ、一時停止、サイズ調整、成行禁止、ヘッジ要請などのフラグ

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from hl_core.utils.logger import get_logger

logger = get_logger("VRLG.risk")


def _safe(cfg, section: str, key: str, default):
    """〔この関数がすること〕 設定オブジェクト/辞書の両対応で値を安全に取得します。"""
    try:
        sec = getattr(cfg, section)
        return getattr(sec, key, default)
    except Exception:
        try:
            return cfg[section].get(key, default)  # type: ignore[index]
        except Exception:
            return default


@dataclass
class RiskAdvice:
    """〔このデータクラスがすること〕
    現在の推奨アクション群を 1 つのオブジェクトにまとめます。
    - killswitch: 直ちにフラット & 停止すべき
    - paused_until: 再開可能時刻（秒 since epoch）。None のとき停止していない
    - size_multiplier: 推奨サイズ倍率（例: 0.5）
    - forbid_market: 成行を禁止すべきか
    - deepen_post_only: post-only を現在より深い価格に置くべきか
    - need_hedge: ヘッジ発注が必要か（NetΔ が VaR 対比で過大）
    - reason: 人が読める簡潔な理由
    """

    killswitch: bool = False
    paused_until: Optional[float] = None
    size_multiplier: float = 1.0
    forbid_market: bool = False
    deepen_post_only: bool = False
    need_hedge: bool = False
    reason: str = ""


class RiskManager:
    """〔このクラスがすること〕
    ルールベースの監視→助言（アクション）を提供します。
    - update_block_interval(): ブロック間隔の最新値を報告
    - register_order_post(): 自分の板提示（display）での板消費率を記録
    - register_fill(): 約定の滑り（ticks）を記録
    - register_stopout(): 損切り発生を記録
    - update_exposure(): NetΔ と VaR₁ₛ を報告
    - advice(): 現時点の推奨アクションを返す
    - should_pause(): 一時停止またはキルスイッチが必要かを返します
    """

    def __init__(self, cfg) -> None:
        """〔このメソッドがすること〕 コンフィグから閾値を読み込み、内部バッファを初期化します。"""
        # しきい値（TOML と同義の既定値）
        self.max_slip_ticks: float = float(_safe(cfg, "risk", "max_slippage_ticks", 1.0))
        self.max_book_impact: float = float(_safe(cfg, "risk", "max_book_impact", 0.02))  # 2%/5s
        self.time_stop_ms: int = int(_safe(cfg, "risk", "time_stop_ms", 1200))
        self.stop_ticks: float = float(_safe(cfg, "risk", "stop_ticks", 3.0))
        self._impact_window_s: float = 5.0

        # 内部状態
        self._block_intervals: Deque[float] = deque(maxlen=120)  # 直近 ~10 分相当まで
        self._impact_events: Deque[tuple[float, float]] = deque()  # (t, impact_fraction)
        self._slip_stream: Deque[tuple[float, float]] = deque()  # (t, slip_ticks)
        self._stopouts: Deque[float] = deque()  # 損切り時刻
        self._paused_until: Optional[float] = None
        self._killswitch: bool = False
        self._forbid_market: bool = False
        self._deepen_post_only: bool = False
        self._size_multiplier: float = 1.0
        self._need_hedge: bool = False
        self._last_reason: str = ""

    # ───────────────────────── 監視系の入力メソッド ─────────────────────────

    def update_block_interval(self, interval_s: float) -> None:
        """〔このメソッドがすること〕
        新しいブロック間隔（秒）を追加し、移動中央値が 4 秒を超えたらキルスイッチを発火します。
        """

        self._block_intervals.append(float(interval_s))
        med = self._median(self._block_intervals)
        if med is not None and med > 4.0:
            self._killswitch = True
            self._last_reason = f"block_interval_median={med:.2f}s > 4s"

    def register_order_post(self, display_size: float, top_depth: float) -> None:
        """〔このメソッドがすること〕
        アイスバーグの display など「見えている提示量」を登録し、TopDepth に対する消費率を記録します。
        後で 5 秒の合計が 2% を超えるとサイズ半減の助言につながります。
        """

        if top_depth <= 0:
            return
        impact = float(display_size) / float(top_depth)  # 例: 0.005 = 0.5%
        self._impact_events.append((time.time(), impact))
        self._trim_impacts()

    def register_fill(self, fill_price: float, ref_mid: float, tick_size: float) -> None:
        """〔このメソッドがすること〕
        約定の滑りを ticks 単位で計算して 1 分の移動平均に反映します。
        しきい値を超えたら「成行禁止」「post‑only を深める」を提案します。
        """

        if tick_size <= 0:
            return
        slip_ticks = abs(float(fill_price) - float(ref_mid)) / float(tick_size)
        self._slip_stream.append((time.time(), slip_ticks))
        self._trim_slippage()

    def book_impact_sum_5s(self) -> float:
        """〔このメソッドがすること〕
        直近5秒の板消費率（display/TopDepth）の合計を返します（Gauge更新用）。
        内部のイベントをトリムしてから合計します。
        """

        self._trim_impacts()
        return float(sum(x for _, x in self._impact_events))

    def register_stopout(self) -> None:
        """〔このメソッドがすること〕
        損切り発生を記録します。10 分内に 3 回で一時停止（10 分）に入ります。
        """

        now = time.time()
        self._stopouts.append(now)
        self._trim_stopouts()
        if len(self._stopouts) >= 3:
            first = self._stopouts[0]
            if (now - first) <= 600.0:
                self._paused_until = now + 600.0
                self._last_reason = "3 stopouts within 10 min → pause 10 min"

    def update_exposure(self, net_delta: float, var_1s: float) -> None:
        """〔このメソッドがすること〕
        バケット全体の NetΔ と VaR₁ₛ を報告し、0.8×VaR₁ₛ を超えたらヘッジ要請フラグを立てます。
        """

        try:
            self._need_hedge = (abs(float(net_delta)) > 0.8 * float(var_1s))
        except Exception:
            self._need_hedge = False

    # ───────────────────────── 助言・状態系の出力メソッド ─────────────────────────

    def advice(self) -> RiskAdvice:
        """〔このメソッドがすること〕
        現在の観測に基づく推奨アクション（RiskAdvice）を返します。
        ここで各ルールを評価して、サイズ倍率や成行禁止をまとめます。
        """

        now = time.time()

        # 1) 板消費率（5 秒合計）
        self._trim_impacts()
        impact_sum = sum(x for _, x in self._impact_events)
        size_mult = 0.5 if impact_sum > self.max_book_impact else 1.0
        if size_mult < 1.0:
            self._last_reason = f"book_impact_5s={impact_sum:.3%} > {self.max_book_impact:.1%}"

        # 2) 滑り（1 分平均）
        self._trim_slippage()
        slip_avg = self._mean([x for _, x in self._slip_stream], window_name="slip_1m")
        forbid_mkt = False
        deepen_po = False
        if slip_avg is not None and slip_avg > self.max_slip_ticks:
            forbid_mkt = True
            deepen_po = True
            self._last_reason = f"avg_slippage_1m={slip_avg:.2f} ticks > {self.max_slip_ticks:.2f}"

        # 3) 連続損切り（10 分）
        self._trim_stopouts()
        paused_until = self._paused_until
        if paused_until is not None and now >= paused_until:
            # 自動解除
            self._paused_until = None
            paused_until = None

        # 4) キルスイッチは update_block_interval で更新済み
        advice = RiskAdvice(
            killswitch=self._killswitch,
            paused_until=paused_until,
            size_multiplier=size_mult,
            forbid_market=forbid_mkt,
            deepen_post_only=deepen_po,
            need_hedge=self._need_hedge,
            reason=self._last_reason,
        )
        return advice

    def should_pause(self) -> bool:
        """〔このメソッドがすること〕
        一時停止すべき（pause 中 or kill 中）かを真偽で返します。
        """

        if self._killswitch:
            return True
        if self._paused_until is None:
            return False
        return time.time() < self._paused_until

    def reset(self) -> None:
        """〔このメソッドがすること〕
        すべての内部フラグとバッファをクリアします（テスト/再起動用）。
        """

        self._block_intervals.clear()
        self._impact_events.clear()
        self._slip_stream.clear()
        self._stopouts.clear()
        self._paused_until = None
        self._killswitch = False
        self._forbid_market = False
        self._deepen_post_only = False
        self._size_multiplier = 1.0
        self._need_hedge = False
        self._last_reason = ""

    # ───────────────────────── 内部ユーティリティ ─────────────────────────

    def _trim_impacts(self) -> None:
        """〔このメソッドがすること〕 5 秒ウィンドウから外れた板消費率要素を捨てます。"""

        now = time.time()
        cut = now - self._impact_window_s
        while self._impact_events and self._impact_events[0][0] < cut:
            self._impact_events.popleft()

    def _trim_slippage(self) -> None:
        """〔このメソッドがすること〕 1 分ウィンドウから外れた滑り要素を捨てます。"""

        now = time.time()
        cut = now - 60.0
        while self._slip_stream and self._slip_stream[0][0] < cut:
            self._slip_stream.popleft()

    def _trim_stopouts(self) -> None:
        """〔このメソッドがすること〕 10 分ウィンドウから外れた損切り記録を捨てます。"""

        now = time.time()
        cut = now - 600.0
        while self._stopouts and self._stopouts[0] < cut:
            self._stopouts.popleft()

    @staticmethod
    def _median(dq: Deque[float]) -> Optional[float]:
        """〔この関数がすること〕 Deque の中央値を返します（空なら None）。"""

        if not dq:
            return None
        return float(statistics.median(dq))

    @staticmethod
    def _mean(arr: list[float], window_name: str = "") -> Optional[float]:
        """〔この関数がすること〕 配列の平均を返します（空なら None）。"""

        if not arr:
            return None
        try:
            return float(sum(arr) / len(arr))
        except Exception as e:  # pragma: no cover
            logger.debug("mean(%s) failed: %s", window_name, e)
            return None
