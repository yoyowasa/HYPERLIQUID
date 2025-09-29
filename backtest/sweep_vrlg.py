# 〔このスクリプトがすること〕
# VRLG の主要ハイパラ（signal.x, signal.y, exec.order_ttl_ms）を
# グリッドまたは Optuna でスイープし、backtest/VRLGSimulator で評価します。
# 目的関数（近似）: score = Sharpe_per_trade − 0.5 × (MaxDD_bps / 10)
# ※ Optuna が無い環境でもグリッドサーチで動きます（必須依存なし）。

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

# 〔この import がすること〕 VRLG のバックテスト基盤を再利用します
try:
    from backtest.vrlg_sim import VRLGSimulator, load_level2_stream  # type: ignore
except Exception:
    from .vrlg_sim import VRLGSimulator, load_level2_stream  # type: ignore


def _load_config(path: str) -> Dict[str, Any]:
    """〔この関数がすること〕 設定ファイル（TOML/YAML）を読み込み dict を返します。"""
    try:
        from hl_core.utils.config import load_config  # type: ignore
        return load_config(path)  # type: ignore[return-value]
    except Exception:
        pass
    # TOML フォールバック（Python 3.11+）
    try:
        import tomllib  # type: ignore
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        import tomli  # type: ignore
        with open(path, "rb") as f:
            return tomli.load(f)


def _deepcopy_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """〔この関数がすること〕 設定 dict をディープコピーします（試行ごとに安全に改変するため）。"""
    return json.loads(json.dumps(cfg))


def _apply_params(cfg: Dict[str, Any], x: float, y: float, ttl_s: float) -> Dict[str, Any]:
    """〔この関数がすること〕 ハイパラ (x, y, ttl_s) を設定 dict に反映して返します。"""
    c = _deepcopy_cfg(cfg)
    c.setdefault("signal", {})
    c.setdefault("exec", {})
    c["signal"]["x"] = float(x)
    c["signal"]["y"] = float(y)
    c["exec"]["order_ttl_ms"] = int(round(ttl_s * 1000.0))
    return c


def _sharpe_per_trade(pnl_bps: List[float]) -> float:
    """〔この関数がすること〕 取引ごとの bps リターンから簡易 Sharpe（√N スケーリング）を返します。"""
    n = len(pnl_bps)
    if n == 0:
        return 0.0
    mu = sum(pnl_bps) / n
    var = sum((x - mu) ** 2 for x in pnl_bps) / max(1, n - 1)
    sd = math.sqrt(max(0.0, var))
    if sd == 0:
        return float("inf") if mu > 0 else 0.0
    return (mu / sd) * math.sqrt(n)


def _objective_from_summary(s: Dict[str, Any]) -> float:
    """〔この関数がすること〕 VRLGSimulator.summary() から目的関数値を計算します。"""
    n = int(s.get("trades", 0))
    if n == 0:
        return -1e9
    sharpe = _sharpe_per_trade([s.get("avg_pnl_bps", 0.0)])  # 近似（集計のみ保持時の保険）
    # 可能なら全トレード平均/分散で厳密化：summary() が将来トレード配列を返すなら差し替え
    sharpe = float(s.get("avg_pnl_bps", 0.0)) / max(1e-9, s.get("median_pnl_bps", 1.0)) if sharpe == 0 else sharpe
    dd_pen = 0.5 * float(s.get("max_drawdown_bps", 0.0)) / 10.0
    return sharpe - dd_pen


def _evaluate(cfg: Dict[str, Any], data: List[Tuple[float, float, float, float, float]]) -> Tuple[float, Dict[str, Any]]:
    """〔この関数がすること〕 1 組の設定でシミュレータを実行し、(score, summary) を返します。"""
    sim = VRLGSimulator(cfg)
    sim.run(iter(data))
    s = sim.summary()
    return _objective_from_summary(s), s


def _grid(params_x: Iterable[float], params_y: Iterable[float], params_ttl: Iterable[float]) -> List[Tuple[float, float, float]]:
    """〔この関数がすること〕 グリッド（x × y × ttl）を列挙して返します。"""
    combos = []
    for x in params_x:
        for y in params_y:
            for t in params_ttl:
                combos.append((float(x), float(y), float(t)))
    return combos


def parse_args() -> argparse.Namespace:
    """〔この関数がすること〕 CLI 引数を解釈します。"""
    p = argparse.ArgumentParser(description="VRLG hyperparameter sweep (grid / optuna)")
    p.add_argument("--config", required=True, help="strategy config (TOML/YAML)")
    p.add_argument("--data-dir", required=True, help="directory containing level2-*.jsonl or *.parquet")
    p.add_argument("--max-rows", type=int, default=0, help="limit number of L2 rows for quick run (0=all)")
    p.add_argument("--mode", choices=["grid", "optuna"], default="grid", help="sweep mode")
    p.add_argument("--trials", type=int, default=30, help="number of trials for optuna")
    p.add_argument("--seed", type=int, default=42, help="random seed for optuna")
    # 既定グリッド（設計書の例）
    p.add_argument("--grid-x", nargs="*", type=float, default=[0.15, 0.2, 0.25, 0.3], help="grid for signal.x")
    p.add_argument("--grid-y", nargs="*", type=float, default=[1.0, 2.0, 3.0], help="grid for signal.y (ticks)")
    p.add_argument("--grid-ttl", nargs="*", type=float, default=[0.6, 1.0, 1.4], help="grid for TTL seconds")
    return p.parse_args()


def main() -> None:
    """〔この関数がすること〕 スイープを実行し、ベスト組合せと上位結果を表示します。"""
    args = parse_args()
    base_cfg = _load_config(args.config)

    # データを一度だけ読込み → メモリ上に保持（試行間で再利用）
    data_iter = load_level2_stream(Path(args.data_dir))
    data = list(data_iter)  # 容量が大きい場合は --max-rows で制限
    if args.max_rows and args.max_rows > 0:
        data = data[: args.max_rows]

    results: List[Tuple[float, Tuple[float, float, float], Dict[str, Any]]] = []

    if args.mode == "grid":
        for x, y, ttl in _grid(args.grid_x, args.grid_y, args.grid_ttl):
            cfg_try = _apply_params(base_cfg, x, y, ttl)
            score, summ = _evaluate(cfg_try, data)
            results.append((score, (x, y, ttl), summ))
    else:
        # Optuna があれば TPE、無ければグリッドへフォールバック
        try:
            import optuna  # type: ignore

            def _objective(trial: "optuna.trial.Trial") -> float:
                x = trial.suggest_float("x", 0.10, 0.35)
                y = trial.suggest_categorical("y", [1.0, 2.0, 3.0])
                ttl = trial.suggest_float("ttl_s", 0.4, 1.6)
                cfg_try = _apply_params(base_cfg, x, y, ttl)
                score, summ = _evaluate(cfg_try, data)
                # trial.user_attrs に要約を入れておくと後で取り出しやすい
                trial.set_user_attr("summary", summ)
                return score

            sampler = optuna.samplers.TPESampler(seed=args.seed)
            study = optuna.create_study(direction="maximize", sampler=sampler)
            study.optimize(_objective, n_trials=args.trials)

            # 結果取り出し
            for t in study.trials:
                params = (float(t.params.get("x", 0.0)), float(t.params.get("y", 0.0)), float(t.params.get("ttl_s", 0.0)))
                summ = t.user_attrs.get("summary", {})
                results.append((t.value if t.value is not None else -1e9, params, summ))
        except Exception:
            # フォールバック: グリッドへ
            for x, y, ttl in _grid(args.grid_x, args.grid_y, args.grid_ttl):
                cfg_try = _apply_params(base_cfg, x, y, ttl)
                score, summ = _evaluate(cfg_try, data)
                results.append((score, (x, y, ttl), summ))

    # スコア順に並べ替え＆上位を表示
    results.sort(key=lambda r: r[0], reverse=True)
    top = results[:10]
    print("Top results (score, x, y, ttl_s, trades, hit, avg_pnl_bps, slip_ticks, hold_ms, TPM, MaxDD):")
    for sc, (x, y, ttl), s in top:
        print("{:8.3f} | x={:.3f} y={:.1f} ttl={:.1f} | n={} hit={:.1%} pnl={:.2f}bps slip={:.2f} h={:.0f}ms tpm={:.2f} dd={:.1f}bps".format(
            sc,
            x, y, ttl,
            s.get("trades", 0),
            s.get("hit_rate", 0.0),
            s.get("avg_pnl_bps", 0.0),
            s.get("avg_slip_ticks", 0.0),
            s.get("avg_holding_ms", 0.0),
            s.get("trades_per_min", 0.0),
            s.get("max_drawdown_bps", 0.0),
        ))

    if results:
        best = results[0]
        sc, (x, y, ttl), s = best
        print("\nBEST:")
        print(json.dumps({
            "score": sc,
            "params": {"x": x, "y": y, "ttl_s": ttl},
            "summary": s,
            "suggested_patch": {
                "signal": {"x": x, "y": y},
                "exec": {"order_ttl_ms": int(round(ttl * 1000.0))}
            }
        }, indent=2))
        print("\nApply these into configs/strategy.toml under [signal]/[exec].")


if __name__ == "__main__":
    main()
