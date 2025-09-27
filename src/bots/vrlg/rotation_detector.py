from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

from hl_core.utils.logger import get_logger

logger = get_logger("VRLG.rotation")


@dataclass
class RotationEstimation:
    """〔このデータクラスがすること〕
    直近の推定結果と品質指標をひとまとめに保持します。
    """

    period_s: Optional[float]
    score: float
    n_boundary: int
    n_off: int
    p_dob: Optional[float]
    p_spread: Optional[float]
    ts: float


def _pearson_corr(a: list[float], b: list[float]) -> float:
    """〔この関数がすること〕
    a,b（同長）のピアソン相関を計算します。分散ゼロのときは 0 を返します。
    """

    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    ma = sum(a) / n
    mb = sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0.0 or vb <= 0.0:
        return 0.0
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


def _normal_sf(x: float) -> float:
    """〔この関数がすること〕
    標準正規分布の片側上側確率（survival function）を返します。
    p = 1 - Phi(x) ≒ 0.5 * erfc(x / sqrt(2))
    """

    return 0.5 * math.erfc(x / math.sqrt(2.0))


def _welch_t_onesided(mean_a: float, var_a: float, n_a: int,
                      mean_b: float, var_b: float, n_b: int,
                      alt: str) -> float:
    """〔この関数がすること〕
    Welch の t を正規近似で片側検定します（SciPy 非依存の軽量近似）。
    alt: "a<b" のとき H1: mean_a < mean_b（左側）、"a>b" のとき H1: mean_a > mean_b（右側）
    戻り値: 片側 p 値（小さいほど有意）
    """

    if n_a <= 1 or n_b <= 1:
        return 1.0
    var_a = max(var_a, 1e-12)
    var_b = max(var_b, 1e-12)
    se = math.sqrt(var_a / n_a + var_b / n_b)
    if se <= 0.0:
        return 1.0
    t = (mean_a - mean_b) / se
    if alt == "a<b":
        return 1.0 - (0.5 * math.erfc(t / math.sqrt(2.0)))
    else:
        return _normal_sf(t)


class RotationDetector:
    """〔このクラスがすること〕
    直近 T_roll 秒の DoB / Spread を保持し、自己相関で周期 R* を推定。
    位相ゲート（z）近傍で「DoB が薄い」「Spread が広い」が十分に再現される
    （p 値がしきい値未満 & サンプル閾以上）とき is_active() を True にします。
    """

    def __init__(self, cfg) -> None:

        """〔このメソッドがすること〕
        パラメータ（T_roll, z, 期間レンジ, p値しきい値, 必要サンプル数）を設定から読み込みます。
        """

        sig = getattr(cfg, "signal", {})
        self.T_roll: float = float(getattr(sig, "T_roll", 30.0))
        self.z: float = float(getattr(sig, "z", 0.6))
        self.period_min_s = float(getattr(sig, "period_min_s", 0.8))
        self.period_max_s = float(getattr(sig, "period_max_s", 5.0))
        self.p_thresh = float(getattr(sig, "p_thresh", 0.01))
        self.min_boundary_samples = int(getattr(sig, "min_boundary_samples", 200))
        self.min_off_samples = int(getattr(sig, "min_off_samples", 50))

        self._dt: float = 0.1
        self._dob: Deque[float] = deque(maxlen=int(self.T_roll / self._dt) + 8)
        self._spr: Deque[float] = deque(maxlen=int(self.T_roll / self._dt) + 8)
        self._tim: Deque[float] = deque(maxlen=int(self.T_roll / self._dt) + 8)


        self._period_s: Optional[float] = None
        self._active: bool = False
        self._score: float = 0.0
        self._p_dob: Optional[float] = None
        self._p_spr: Optional[float] = None
        self._n_on: int = 0
        self._n_off: int = 0
        self._last_est = RotationEstimation(None, 0.0, 0, 0, None, None, time.time())

    def update(self, t: float, dob: float, spread_ticks: float) -> None:
        """〔このメソッドがすること〕
        新しい観測（時刻・DoB・Spread）を保持し、必要なら R* と品質を再推定します。
        """

        self._tim.append(float(t))
        self._dob.append(float(dob))
        self._spr.append(float(spread_ticks))

        if len(self._tim) < max(20, int(self.period_max_s / self._dt) + 5):
            self._set_inactive("insufficient data")
            return

        self._estimate_period_and_quality()

    def is_active(self) -> bool:
        """〔このメソッドがすること〕 構造が有意に観測できている（稼働可能）かを返します。"""

        return bool(self._active)

    def current_period(self) -> Optional[float]:
        """〔このメソッドがすること〕 推定した周期 R*（秒）を返します（未確定なら None）。"""

        return self._period_s

    def current_phase(self, t: float) -> float:
        """〔このメソッドがすること〕 与えた時刻 t に対する位相（0..1）を返します。"""

        p = self._period_s or 1.0
        if p <= 0.0:
            p = 1.0
        ph = (float(t) % p) / p
        if ph >= 1.0:
            ph -= 1.0
        if ph < 0.0:
            ph += 1.0
        return ph

    def last_estimation(self) -> RotationEstimation:
        """〔このメソッドがすること〕 直近の推定結果と品質指標を返します。"""


        return self._last_est


    def _estimate_period_and_quality(self) -> None:
        """〔このメソッドがすること〕
        候補周期レンジで自己相関スコアを評価し、最良の R* を選びます。
        その R* に基づいて位相ゲート境界/非境界での DoB/Spread の差を検定し、active を更新します。
        """

        dob = list(self._dob)
        spr = list(self._spr)
        tim = list(self._tim)
        n = len(tim)
        if n < 5:
            self._set_inactive("insufficient data")
            return

        Lmin = max(1, int(round(self.period_min_s / self._dt)))
        Lmax = max(Lmin + 1, int(round(self.period_max_s / self._dt)))
        best_score = -1.0
        best_L = None

        for L in range(Lmin, min(Lmax, n - 2)):
            a_dob = dob[:-L]
            b_dob = dob[L:]
            a_spr = spr[:-L]
            b_spr = spr[L:]
            if len(a_dob) < 5 or len(a_spr) < 5:
                continue
            r_d = abs(_pearson_corr(a_dob, b_dob))
            r_s = abs(_pearson_corr(a_spr, b_spr))
            score = 0.5 * (r_d + r_s)
            if score > best_score:
                best_score = score
                best_L = L

        if best_L is None:
            self._set_inactive("no lag chosen")
            return

        R = best_L * self._dt
        self._period_s = R
        self._score = float(best_score)

        z = max(0.01, min(self.z, 0.45))
        on_dob, off_dob = [], []
        on_spr, off_spr = [], []
        for i in range(n):
            ph = (tim[i] % R) / R
            boundary = (ph < z) or (ph > 1.0 - z)
            if boundary:
                on_dob.append(dob[i])
                on_spr.append(spr[i])
            else:
                off_dob.append(dob[i])
                off_spr.append(spr[i])

        n_on = len(on_dob)
        n_off = len(off_dob)
        self._n_on, self._n_off = n_on, n_off

        if n_on < self.min_boundary_samples or n_off < self.min_off_samples:
            self._p_dob = None
            self._p_spr = None
            self._set_inactive("insufficient samples")
            return


        def _mean_var(xs: list[float]) -> Tuple[float, float]:
            if not xs:
                return 0.0, 0.0
            m = sum(xs) / len(xs)
            v = sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)
            return m, v

        m_on_d, v_on_d = _mean_var(on_dob)
        m_off_d, v_off_d = _mean_var(off_dob)
        m_on_s, v_on_s = _mean_var(on_spr)
        m_off_s, v_off_s = _mean_var(off_spr)

        p_d = _welch_t_onesided(m_on_d, v_on_d, n_on, m_off_d, v_off_d, n_off, alt="a<b")
        p_s = _welch_t_onesided(m_on_s, v_on_s, n_on, m_off_s, v_off_s, n_off, alt="a>b")
        self._p_dob = float(p_d)
        self._p_spr = float(p_s)

        self._active = (p_d < self.p_thresh) and (p_s < self.p_thresh)

        self._last_est = RotationEstimation(
            period_s=self._period_s,
            score=self._score,
            n_boundary=n_on,
            n_off=n_off,
            p_dob=self._p_dob,
            p_spread=self._p_spr,
            ts=time.time(),
        )

    def _set_inactive(self, reason: str) -> None:
        """〔このメソッドがすること〕 active を False にし、推定結果を更新します（内部用）。"""

        self._active = False
        self._last_est = RotationEstimation(
            period_s=self._period_s,
            score=self._score,
            n_boundary=self._n_on,
            n_off=self._n_off,
            p_dob=self._p_dob,
            p_spread=self._p_spr,
            ts=time.time(),
        )

        logger.debug("rotation inactive: %s", reason)
