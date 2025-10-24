from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional
from uuid import uuid4


import logging

from .data_feed import FeatureSnapshot

logger = logging.getLogger("bots.vrlg.signal")


@dataclass(frozen=True)
class Signal:
    """〔このデータクラスがすること〕 シグナル発火時の最小情報（相関ID付き）を表します。"""

    t: float
    mid: float
    trace_id: str  # trace: シグナル→発注→解消を結ぶ相関ID（Step40で配線）



def _get(section: object, key: str, default):
    """〔この関数がすること〕 設定が dict/オブジェクトいずれでも値を安全に取り出します。"""

    try:
        value = getattr(section, key)
        return value if value is not None else default
    except Exception:
        try:
            return section[key]  # type: ignore[index]
        except Exception:
            try:
                getter = getattr(section, "get")
                return getter(key, default)
            except Exception:
                return default


class SignalDetector:
    """〔このクラスがすること〕
    4 条件ゲートを評価して、成立時に Signal を返します。
    - update_and_maybe_signal(t, features) -> Optional[Signal]
    """

    def __init__(self, cfg) -> None:
        """〔このメソッドがすること〕 設定（N, x, y, z, obi_limit）を読み、内部バッファを初期化します。"""

        sig = getattr(cfg, "signal", None)
        if sig is None:
            try:
                sig = cfg["signal"]  # type: ignore[index]
            except Exception:
                sig = {}
        self.N: int = int(_get(sig, "N", 80))
        self.x: float = float(_get(sig, "x", 0.25))
        self.y: float = float(_get(sig, "y", 2.0))
        self.z: float = float(_get(sig, "z", 0.6))
        self.obi_limit: float = float(_get(sig, "obi_limit", 0.6))

        # DoB の中央値計算用バッファ
        self._dob_hist: Deque[float] = deque(maxlen=max(1, self.N))

        # ゲート評価通知（Step39 で Strategy に配線）
        self.on_gate_eval: Optional[Callable[[dict], None]] = None

    def _median_dob(self) -> float:
        """〔この関数がすること〕 DoB 履歴の中央値を返します（空なら 0）。"""

        if not self._dob_hist:
            return 0.0
        try:
            return float(statistics.median(self._dob_hist))
        except Exception:
            arr = sorted(self._dob_hist)
            n = len(arr)
            k = n // 2
            if n % 2 == 1:
                return float(arr[k])
            return float(0.5 * (arr[k - 1] + arr[k]))

    def update_and_maybe_signal(self, t: float, features: FeatureSnapshot) -> Optional[Signal]:
        """〔このメソッドがすること〕
        特徴量を 1 つ受け取り、4 条件ゲートを評価します。成立したら Signal を返します。
        """

        # DoB 履歴を更新（最新を末尾へ）
        self._dob_hist.append(float(features.dob))

        # しきい値の前計算
        med_dob = self._median_dob()
        dob_thin = False if med_dob <= 0.0 else (float(features.dob) < med_dob * (1.0 - self.x))
        spread_ok = float(features.spread_ticks) >= self.y
        obi_ok = abs(float(features.obi)) <= self.obi_limit

        # 位相ゲート（features.block_phase が無ければ不成立）
        z = max(0.01, min(self.z, 0.45))
        phase = features.block_phase if features.block_phase is not None else -1.0
        phase_gate = (0.0 <= phase <= 1.0) and (phase < z or phase > 1.0 - z)

        # ゲート評価の通知（必要なら）
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

        # 4 条件の同時成立
        if phase_gate and dob_thin and spread_ok and obi_ok:
            return Signal(t=float(t), mid=float(features.mid), trace_id=uuid4().hex[:12])

        return None
