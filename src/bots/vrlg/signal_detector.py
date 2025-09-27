from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from typing import Deque, Optional, Callable  # 〔この import がすること〕 ゲート評価通知のコールバック型を使う


import uuid  # 〔この行がすること〕 trace_id を生成するための標準ライブラリを使います


from hl_core.utils.logger import get_logger
from .data_feed import FeatureSnapshot

logger = get_logger("VRLG.signal")


@dataclass(frozen=True)
class Signal:
    """〔このデータクラスがすること〕
    シグナル発火の事実を表します。
    t: 生成時刻（秒）
    mid: 発火時点のミッド価格
    trace_id: 後続の発注・キャンセル・解消イベントと紐づけるための相関ID
    """
    t: float
    mid: float
    trace_id: str  # 〔このフィールドがすること〕 シグナル→発注→解消までの相関ID


def _safe_get(cfg, section: str, key: str, default):
    """〔この関数がすること〕
    設定オブジェクト/辞書の両対応で値を安全に取得します。
    """
    try:
        sec = getattr(cfg, section)
        return getattr(sec, key, default)
    except Exception:
        try:
            return cfg[section].get(key, default)  # type: ignore[index]
        except Exception:
            return default


def _median_from_deque(dq: Deque[float]) -> float:
    """〔この関数がすること〕
    Deque の値から中央値を求めます（N が小さいため単純ソートで十分）。
    """
    n = len(dq)
    if n == 0:
        return 0.0
    arr = sorted(dq)
    mid = n // 2
    if n % 2:
        return float(arr[mid])
    return float((arr[mid - 1] + arr[mid]) / 2.0)


class SignalDetector:
    """〔このクラスがすること〕
    4つの条件（位相ゲート/DoBの薄さ/Spreadの広さ/OBIの上限）を評価し、
    すべて満たしたタイミングで Signal を返します。

    条件:
      1) 位相ゲート: phase ∈ {0±z} ∪ {1±z}  （z は半幅の比）
      2) DoB: dob < median(DoB_{t-N:t}) × (1 - x)
      3) Spread: spread_ticks ≥ y
      4) OBI: |obi| ≤ obi_limit
    """

    def __init__(self, cfg) -> None:
        """〔このメソッドがすること〕
        パラメータ（N,x,y,z,obi_limit）を設定から読み取り、DoBのローリング窓を初期化します。
        """
        self.N: int = int(_safe_get(cfg, "signal", "N", 80))
        self.x: float = float(_safe_get(cfg, "signal", "x", 0.25))
        self.y: float = float(_safe_get(cfg, "signal", "y", 2.0))
        # z は位相の半幅（0〜1の比）。安全のためクリップ。
        self.z: float = max(0.01, min(float(_safe_get(cfg, "signal", "z", 0.6)), 0.45))
        self.obi_limit: float = float(_safe_get(cfg, "signal", "obi_limit", 0.6))

        self._dob_hist: Deque[float] = deque(maxlen=self.N)
        # 〔この属性がすること〕 ゲート評価結果を上位（Strategy 等）へ通知するためのコールバック
        self.on_gate_eval: Optional[Callable[[dict], None]] = None

    def update_and_maybe_signal(self, t: float, features: FeatureSnapshot) -> Optional[Signal]:
        """〔このメソッドがすること〕
        新しい特徴量を取り込み、4条件を評価し、成立すれば Signal を返します。
        成立しなければ None を返します。
        """
        # DoB ヒストリー更新
        self._dob_hist.append(float(features.dob))
        if len(self._dob_hist) < self.N:
            return None  # 統計が安定するまで待機

        # 1) 位相ゲート（RotationDetector が block_phase を埋める前提）
        phase = features.block_phase
        if phase is None:
            return None
        phase_gate = (phase < self.z) or (phase > 1.0 - self.z)

        # 2) DoB がローリング中央値より x 分だけ薄い
        dob_med = _median_from_deque(self._dob_hist)
        dob_thin = features.dob < (dob_med * (1.0 - self.x))

        # 3) スプレッドが y ティック以上
        spread_ok = features.spread_ticks >= self.y

        # 4) 板不均衡が閾値以下（極端な片寄りを避ける）
        obi_ok = abs(features.obi) <= self.obi_limit

        # 〔このブロックがすること〕 ゲート評価結果を必要なら上位へ通知（観測値も添える）
        try:
            if self.on_gate_eval:
                self.on_gate_eval({
                    "t": float(t),
                    "phase": float(phase),
                    "phase_gate": bool(phase_gate),
                    "dob_thin": bool(dob_thin),
                    "spread_ok": bool(spread_ok),
                    "obi_ok": bool(obi_ok),
                    "mid": float(features.mid),
                    "dob": float(features.dob),
                    "spread_ticks": float(features.spread_ticks),
                    "obi": float(features.obi),
                })
        except Exception:
            pass

        if phase_gate and dob_thin and spread_ok and obi_ok:
            return Signal(t=t, mid=float(features.mid), trace_id=uuid.uuid4().hex[:12])  # 〔この行がすること〕 一意な相関IDを付けて返します
        return None
