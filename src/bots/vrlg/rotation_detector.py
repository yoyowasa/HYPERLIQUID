from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from hl_core.utils.logger import get_logger

logger = get_logger("VRLG.rotation")


@dataclass(frozen=True)
class PeriodEstimation:
    """〔このデータクラスがすること〕 現在の周期推定の結果を保持します。"""

    period_s: Optional[float]
    score: float
    p_dob: Optional[float]
    p_spread: Optional[float]
    n_boundary: int
    active: bool


class RotationDetector:
    """〔このクラスがすること〕
    - ローリング時系列（DoB, Spread）を保持
    - 自己相関から最強周期 R* を推定
    - R* の境界位相で DoB↓ / Spread↑ の有意性（p≤閾値）を評価
    - 戦略の有効/無効判定（is_active）と、現在の位相（current_phase）を提供
    """

    def __init__(self, cfg) -> None:
        """〔このメソッドがすること〕 設定からウィンドウ長やしきい値を読み込み、バッファを初期化します。"""
        # 設定（存在しない場合に備え安全に既定値）
        self.window_s = float(getattr(getattr(cfg, "signal", {}), "T_roll", 30.0))
        self.dt = 0.1  # 100ms 特徴量クロック想定
        self.period_min_s = 0.8
        self.period_max_s = 5.0
        self.p_thresh = 0.01
        # 位相ウィンドウ幅（半幅の比。物理的に意味が保てるよう [0.01, 0.45] にクランプ）
        z = getattr(getattr(cfg, "signal", {}), "z", 0.6)
        self.phase_halfwidth = min(max(float(z), 0.01), 0.45)

        # ローリングバッファ
        maxlen = int(self.window_s / self.dt) + 4
        self.ts: Deque[float] = deque(maxlen=maxlen)
        self.dob: Deque[float] = deque(maxlen=maxlen)
        self.spr: Deque[float] = deque(maxlen=maxlen)

        # 推定結果
        self._period_s: Optional[float] = None
        self._active: bool = False
        self._last_eval_t: float = 0.0
        self._last_est: PeriodEstimation = PeriodEstimation(None, 0.0, None, None, 0, False)

    def update(self, t: float, dob: float, spread_ticks: float) -> None:
        """〔このメソッドがすること〕 新しい観測を追加し、一定間隔で周期推定と有意性評価を行います。"""
        self.ts.append(t)
        self.dob.append(float(dob))
        self.spr.append(float(spread_ticks))

        # 計算負荷を抑えるため 0.5s ごとに評価
        if (t - self._last_eval_t) >= 0.5:
            self._last_eval_t = t
            self._recompute()

    def current_phase(self, t: float) -> float:
        """〔このメソッドがすること〕 現在時刻 t における位相（0〜1）を返します。R* 未確定なら 0.0。"""
        if not self._period_s or self._period_s <= 0:
            return 0.0
        p = self._period_s
        return (t % p) / p

    def is_active(self) -> bool:
        """〔このメソッドがすること〕 p値・サンプル数の条件を満たしているか（=戦略稼働可）を返します。"""
        return self._active

    def current_period(self) -> Optional[float]:
        """〔このメソッドがすること〕 推定された R*（秒）を返します。未確定なら None。"""
        return self._period_s

    def last_estimation(self) -> PeriodEstimation:
        """〔このメソッドがすること〕 直近の推定詳細（デバッグ/監視用）を返します。"""
        return self._last_est

    # ─────────────────────────── 内部実装 ───────────────────────────

    def _recompute(self) -> None:
        """〔このメソッドがすること〕
        バッファから
        1) 自己相関スコア最大のラグ → R* を推定し、
        2) 境界位相の集合と非境界の集合で DoB/Spread の片側検定を行い、
        3) 稼働可否を更新します。
        """
        if len(self.ts) < 40:
            self._active = False
            self._period_s = None
            self._last_est = PeriodEstimation(None, 0.0, None, None, 0, False)
            return

        # 1) 不規則タイムスタンプを 100ms グリッドに再ビン詰め（ラストホールド）
        grid, xs_dob, xs_spr = self._make_grid()

        # 自己相関スキャン
        lag_min = max(1, int(self.period_min_s / self.dt))
        lag_max = min(len(grid) // 2, int(self.period_max_s / self.dt))
        if lag_max <= lag_min:
            self._active = False
            self._period_s = None
            self._last_est = PeriodEstimation(None, 0.0, None, None, 0, False)
            return

        score_best = -1.0
        lag_best = None
        for L in range(lag_min, lag_max + 1):
            c1 = self._acf(xs_dob, L)
            c2 = self._acf(xs_spr, L)
            score = 0.5 * (abs(c1) + abs(c2))
            if score > score_best:
                score_best = score
                lag_best = L

        if lag_best is None:
            self._active = False
            self._period_s = None
            self._last_est = PeriodEstimation(None, 0.0, None, None, 0, False)
            return

        period_s = lag_best * self.dt

        # 2) 位相境界の統計（DoB: 境界で小さい / Spread: 境界で大きい）
        phases = [((grid[i] % period_s) / period_s) for i in range(len(grid))]
        hw = self.phase_halfwidth
        boundary_idx = [i for i, ph in enumerate(phases) if (ph < hw or ph > 1.0 - hw)]
        off_idx = [i for i, ph in enumerate(phases) if (hw <= ph <= 1.0 - hw)]

        n_on = len(boundary_idx)
        n_off = len(off_idx)
        p_dob = None
        p_spr = None
        active = False

        if n_on >= 200 and n_off >= 50:
            dob_on = [xs_dob[i] for i in boundary_idx]
            dob_off = [xs_dob[i] for i in off_idx]
            spr_on = [xs_spr[i] for i in boundary_idx]
            spr_off = [xs_spr[i] for i in off_idx]

            p_dob = self._one_sided_p_less(dob_on, dob_off)    # on < off で小さければ良い
            p_spr = self._one_sided_p_greater(spr_on, spr_off) # on > off で小さければ良い
            active = (
                p_dob is not None
                and p_spr is not None
                and p_dob <= self.p_thresh
                and p_spr <= self.p_thresh
            )

        self._period_s = period_s
        self._active = bool(active)
        self._last_est = PeriodEstimation(period_s, float(score_best), p_dob, p_spr, n_on, self._active)

    def _make_grid(self):
        """〔このメソッドがすること〕
        時刻・DoB・Spread を 100ms 間隔の等間隔グリッドに再配置し、欠損は前値保持で補完します。
        """
        t_end = self.ts[-1]
        steps = int(self.window_s / self.dt)
        t0 = t_end - steps * self.dt
        grid = [t0 + i * self.dt for i in range(steps)]

        xs_dob = [float("nan")] * steps
        xs_spr = [float("nan")] * steps

        # 直近観測を同じビンに入れる（上書き）
        for ti, di, si in zip(self.ts, self.dob, self.spr):
            idx = int(round((ti - t0) / self.dt))
            if 0 <= idx < steps:
                xs_dob[idx] = di
                xs_spr[idx] = si

        # 前値保持で NaN を埋める（先頭は最初の有限値で埋め）
        def ffill(arr: list[float]) -> None:
            last = None
            # 先頭探し
            for v in arr:
                if not math.isnan(v):
                    last = v
                    break
            if last is None:
                # 全部空なら 0 扱い
                for i in range(len(arr)):
                    arr[i] = 0.0
                return
            # 前値保持
            for i in range(len(arr)):
                if math.isnan(arr[i]):
                    arr[i] = last
                else:
                    last = arr[i]

        ffill(xs_dob)
        ffill(xs_spr)
        return grid, xs_dob, xs_spr

    def _acf(self, x: list[float], lag: int) -> float:
        """〔このメソッドがすること〕 ラグ lag の自己相関係数（ピアソン）を近似計算します。"""
        n = len(x)
        if lag <= 0 or lag >= n:
            return 0.0
        # 中心化
        mu = sum(x) / n
        xv = [xi - mu for xi in x]
        num = 0.0
        den1 = 0.0
        den2 = 0.0
        for i in range(lag, n):
            a = xv[i]
            b = xv[i - lag]
            num += a * b
            den1 += a * a
            den2 += b * b
        den = math.sqrt(den1 * den2) if den1 > 0 and den2 > 0 else 0.0
        return (num / den) if den > 0 else 0.0

    def _one_sided_p_less(self, a_on: list[float], a_off: list[float]) -> Optional[float]:
        """〔このメソッドがすること〕
        片側検定（on < off）を正規近似で行い、p 値を返します（失敗時 None）。
        """
        return self._ztest_one_sided(
            mean_a=sum(a_on) / len(a_on),
            var_a=self._variance(a_on),
            n_a=len(a_on),
            mean_b=sum(a_off) / len(a_off),
            var_b=self._variance(a_off),
            n_b=len(a_off),
            direction="less",
        )

    def _one_sided_p_greater(self, a_on: list[float], a_off: list[float]) -> Optional[float]:
        """〔このメソッドがすること〕
        片側検定（on > off）を正規近似で行い、p 値を返します（失敗時 None）。
        """
        return self._ztest_one_sided(
            mean_a=sum(a_on) / len(a_on),
            var_a=self._variance(a_on),
            n_a=len(a_on),
            mean_b=sum(a_off) / len(a_off),
            var_b=self._variance(a_off),
            n_b=len(a_off),
            direction="greater",
        )

    @staticmethod
    def _variance(arr: list[float]) -> float:
        """〔この関数がすること〕 不偏分散を計算します（要素数<2なら小さな値を返す）。"""
        n = len(arr)
        if n < 2:
            return 1e-12
        mu = sum(arr) / n
        s2 = sum((x - mu) ** 2 for x in arr) / (n - 1)
        return max(s2, 1e-12)

    @staticmethod
    def _z_to_p_one_sided(z: float) -> float:
        """〔この関数がすること〕 片側 p 値を正規分布の尾確率から計算します。"""
        # 片側: p = 1 - Phi(z) = 0.5 * erfc(z / sqrt(2))
        return 0.5 * math.erfc(z / math.sqrt(2.0))

    def _ztest_one_sided(
        self,
        mean_a: float,
        var_a: float,
        n_a: int,
        mean_b: float,
        var_b: float,
        n_b: int,
        direction: str,
    ) -> Optional[float]:
        """〔この関数がすること〕
        2標本の片側 z 検定（正規近似; Welch）を行い、p 値を返します。
        direction: "less"（a<b を検出） or "greater"（a>b を検出）
        """
        if n_a < 2 or n_b < 2:
            return None
        se = math.sqrt(var_a / n_a + var_b / n_b)
        if se <= 0:
            return None
        if direction == "less":
            z = (mean_b - mean_a) / se
        else:
            z = (mean_a - mean_b) / se
        return self._z_to_p_one_sided(z)
