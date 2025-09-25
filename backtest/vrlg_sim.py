# 〔このモジュールがすること〕
# 100ms ステップの簡易シミュレータです。VRLG の「周期検出→シグナル→擬似約定→指標集計」を行います。
# 録画データ（parquet）が無い場合は合成データを生成して流します。
# 将来は hl_core.utils.backtest の実データイテレータに差し替えるだけで動く構成です。

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Iterable, Optional

from hl_core.utils.logger import get_logger
from hl_core.utils.config import load_config

# 既存の VRLG コンポーネント（位相・シグナル・特徴量）を再利用します
try:
    from bots.vrlg.rotation_detector import RotationDetector
    from bots.vrlg.signal_detector import SignalDetector
    from bots.vrlg.data_feed import FeatureSnapshot
except Exception:  # 実行環境によって import パスが異なる場合のフォールバック
    from src.bots.vrlg.rotation_detector import RotationDetector  # type: ignore
    from src.bots.vrlg.signal_detector import SignalDetector  # type: ignore
    from src.bots.vrlg.data_feed import FeatureSnapshot  # type: ignore

logger = get_logger("VRLG.backtest")


@dataclass
class SimResult:
    """〔このデータクラスがすること〕
    バックテストの主要指標をまとめて保持します。
    """
    trades: int
    fills: int
    win_rate: float
    avg_pnl_bps: float
    avg_slip_ticks: float
    trades_per_min: float
    holding_ms_avg: float
    max_dd_ticks: float


class VRLGBacktester:
    """〔このクラスがすること〕
    VRLG のバックテスト実行器。合成/録画データから 100ms 特徴量を受け取り、
    RotationDetector→SignalDetector を通してシグナルを評価し、簡易ルールで擬似約定します。
    """

    def __init__(self, cfg) -> None:
        """〔このメソッドがすること〕
        コンフィグから必要なパラメータを読み込み、検出器を初期化します。
        """
        self.cfg = cfg
        self.tick = float(getattr(getattr(cfg, "symbol", {}), "tick_size", 0.5))
        self.y = float(getattr(getattr(cfg, "signal", {}), "y", 2.0))
        self.holding_target_ms = 500.0  # 目標ホールド（設計と整合）
        self.rot = RotationDetector(cfg)
        self.sigdet = SignalDetector(cfg)

        # 集計用
        self._pnl_ticks_sum = 0.0
        self._slip_ticks_sum = 0.0
        self._wins = 0
        self._fills = 0
        self._trades = 0
        self._dd_min = 0.0
        self._cum_pnl_ticks = 0.0
        self._t0 = None
        self._t1 = None

    def run(self, source: Optional[Path] = None, duration_s: float = 120.0) -> SimResult:
        """〔このメソッドがすること〕
        バックテストを実行し、SimResult を返します。
        - source が parquet ディレクトリなら録画データを使い、
          そうでなければ duration_s 秒の合成データで走らせます。
        """
        feats: Iterable[FeatureSnapshot]
        if source and source.exists():
            feats = self._iter_recorded_features(source)
        else:
            feats = self._iter_synthetic_features(duration_s)

        for f in feats:
            # 周期更新→位相算出→位相を埋めた特徴量へ
            self.rot.update(f.t, f.dob, f.spread_ticks)
            if not self.rot.is_active():
                continue
            phase = self.rot.current_phase(f.t)
            f = f.with_phase(phase)

            # 4条件ゲート
            sig = self.sigdet.update_and_maybe_signal(f.t, f)
            if not sig:
                continue
            self._trades += 1

            # ――― 簡易フィル判定（骨組み）
            # spreadが閾値より十分に大きいときは高確率でフィルした前提とし、
            # PnL を「0.5tick/トレード」、滑りは 0tick として集計（後で置換）
            filled = (f.spread_ticks >= (self.y + 0.0))
            if filled:
                self._fills += 1
                pnl_ticks = 0.5  # mid±0.5tick→midでIOC解消の想定
                slip_ticks = 0.0
                self._cum_pnl_ticks += pnl_ticks
                self._pnl_ticks_sum += pnl_ticks
                self._slip_ticks_sum += slip_ticks
                self._wins += 1 if pnl_ticks > 0 else 0
                # ドローダウン更新
                self._dd_min = min(self._dd_min, self._cum_pnl_ticks)

            # 時間範囲
            if self._t0 is None:
                self._t0 = f.t
            self._t1 = f.t

        # 指標まとめ
        minutes = max(1e-9, ((self._t1 or 0) - (self._t0 or 0)) / 60.0)
        trades_per_min = self._fills / minutes if minutes > 0 else 0.0
        win_rate = (self._wins / self._fills) if self._fills > 0 else 0.0
        avg_slip = (self._slip_ticks_sum / self._fills) if self._fills > 0 else 0.0

        # bps換算（平均 mid がないので近似: 1 tick / 価格の 1e4 * mid）
        # 骨組み段階では保守的に 0.5tick を mid=100 で換算（= 5 bps 相当）
        avg_pnl_bps = 5.0 if self._fills > 0 else 0.0
        holding_ms_avg = self.holding_target_ms if self._fills > 0 else 0.0
        max_dd_ticks = -self._dd_min

        return SimResult(
            trades=self._trades,
            fills=self._fills,
            win_rate=win_rate,
            avg_pnl_bps=avg_pnl_bps,
            avg_slip_ticks=avg_slip,
            trades_per_min=trades_per_min,
            holding_ms_avg=holding_ms_avg,
            max_dd_ticks=max_dd_ticks,
        )

    def _iter_recorded_features(self, parquet_dir: Path) -> Iterable[FeatureSnapshot]:
        """〔このメソッドがすること〕
        録画済みの parquet データから 100ms 特徴量列を生成します。
        ここではプレースホルダです。hl_core.utils.backtest 側の実装が揃い次第、差し替えてください。
        """
        try:
            # 期待する形（例）：iter_l2_snapshots(parquet_dir, dt=0.1) -> yields dict-like
            from hl_core.utils.backtest import iter_l2_snapshots  # type: ignore
            tick = self.tick
            for s in iter_l2_snapshots(parquet_dir, dt=0.1):  # type: ignore[misc]
                # s: {"t": float, "best_bid": float, "best_ask": float, "bid_size_l1": float, "ask_size_l1": float}
                mid = (s["best_bid"] + s["best_ask"]) / 2.0
                spread_ticks = (s["best_ask"] - s["best_bid"]) / max(tick, 1e-12)
                dob = float(s["bid_size_l1"]) + float(s["ask_size_l1"])
                obi = 0.0 if dob <= 0 else (float(s["bid_size_l1"]) - float(s["ask_size_l1"])) / dob
                yield FeatureSnapshot(t=float(s["t"]), mid=mid, spread_ticks=spread_ticks, dob=dob, obi=obi)
        except Exception as e:
            logger.warning("recorded iterator unavailable (%s). fallback to synthetic.", e)
            yield from self._iter_synthetic_features(120.0)

    def _iter_synthetic_features(self, duration_s: float = 120.0) -> Generator[FeatureSnapshot, None, None]:
        """〔このメソッドがすること〕
        周期 R*=2.0s 付近で「境界が薄い・スプレッドが広い」特徴を持つ合成データを 100ms 間隔で生成します。
        RotationDetector が自力で R* を推定できるよう、DoB/Spread に周期構造を埋め込んでいます。
        """
        dt = 0.1
        t0 = time.time()
        steps = int(duration_s / dt)
        tick = self.tick
        mid0 = 70000.0
        for i in range(steps):
            t = t0 + i * dt
            # 合成の真の周期（RotationDetector が当てる対象）
            R_true = 2.0
            ph = (t % R_true) / R_true  # 0..1
            # 位相境界 15% 付近を「薄い/広い」に
            boundary = (ph < 0.15) or (ph > 0.85)
            spr_ticks = 3.0 if boundary else 1.0
            dob = 600.0 if boundary else 1200.0
            # 中央価格は微小にふらす（正弦波）
            mid = mid0 * (1.0 + 0.00001 * math.sin(2 * math.pi * i / 997))
            # OBI は軽い揺れ（|OBI|<=0.6 内）
            obi = 0.3 * math.sin(2 * math.pi * i / 137)
            yield FeatureSnapshot(t=t, mid=mid, spread_ticks=spr_ticks, dob=dob, obi=obi)

    def to_json(self, res: SimResult) -> str:
        """〔このメソッドがすること〕
        SimResult を人/機械どちらでも読みやすい JSON 文字列にします。
        """
        return json.dumps(
            {
                "trades": res.trades,
                "fills": res.fills,
                "win_rate": round(res.win_rate, 4),
                "avg_pnl_bps": round(res.avg_pnl_bps, 3),
                "avg_slip_ticks": round(res.avg_slip_ticks, 4),
                "trades_per_min": round(res.trades_per_min, 3),
                "holding_ms_avg": round(res.holding_ms_avg, 1),
                "max_dd_ticks": round(res.max_dd_ticks, 3),
            },
            ensure_ascii=False,
        )


def parse_args() -> argparse.Namespace:
    """〔この関数がすること〕
    CLI 引数を解釈します。--config で TOML/YAML、--data に parquet ディレクトリを指定できます。
    """
    p = argparse.ArgumentParser(description="VRLG Backtest (100ms simulator)")
    p.add_argument("--config", required=True, help="path to VRLG config (TOML/YAML)")
    p.add_argument("--data", type=Path, default=None, help="parquet recordings directory (optional)")
    p.add_argument("--duration", type=float, default=120.0, help="synthetic duration seconds when no data")
    return p.parse_args()


def main() -> None:
    """〔この関数がすること〕
    バックテストを実行し、指標を JSON で標準出力へ表示します。
    """
    args = parse_args()
    cfg = load_config(args.config)
    bt = VRLGBacktester(cfg)
    res = bt.run(args.data, duration_s=args.duration)
    print(bt.to_json(res))


if __name__ == "__main__":
    main()
