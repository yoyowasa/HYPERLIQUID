# 〔このモジュールがすること〕
# VRLG の設定（dict など）を「属性アクセスできる dataclass」へ変換します。
# - coerce_vrlg_config(data): dict/オブジェクト → VRLGConfig（symbol/signal/exec/risk/latency）
# - load_vrlg_config(path): hl_core.utils.config.load_config を使って読み込み → coerce に通す

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# ───────────── dataclass 定義（既定値は設計書の例を採用） ─────────────


@dataclass
class SymbolCfg:
    """〔このクラスがすること〕 銘柄関連の設定を保持します。"""

    name: str = "BTCUSD-PERP"
    tick_size: float = 0.5


@dataclass
class SignalCfg:
    """〔このクラスがすること〕 シグナル判定・周期検出の設定を保持します。"""

    N: int = 80
    x: float = 0.25
    y: float = 2.0
    z: float = 0.6
    obi_limit: float = 0.6
    T_roll: float = 30.0
    # RotationDetector 調整（Step42）
    p_thresh: float = 0.01
    period_min_s: float = 0.8
    period_max_s: float = 5.0
    min_boundary_samples: int = 200
    min_off_samples: int = 50


@dataclass
class ExecCfg:
    """〔このクラスがすること〕 執行（置き方/TTL/分割/露出）関連の設定を保持します。"""

    order_ttl_ms: int = 1000
    display_ratio: float = 0.25
    min_display_btc: float = 0.01
    max_exposure_btc: float = 0.8
    cooldown_factor: float = 2.0
    # 置き方（Step31）
    offset_ticks_normal: float = 0.5
    offset_ticks_deep: float = 1.5
    spread_collapse_ticks: float = 1.0
    side_mode: str = "both"  # "both" | "buy" | "sell"
    # 分割（Step43）
    splits: int = 1
    # サイズ割当（Step64）
    percent_min: float = 0.002
    percent_max: float = 0.005
    min_clip_btc: float = 0.001
    equity_usd: float = 10_000.0


@dataclass
class RiskCfg:
    """〔このクラスがすること〕 リスク管理の設定を保持します。"""

    max_slippage_ticks: float = 1.0
    max_book_impact: float = 0.02
    time_stop_ms: int = 1200
    stop_ticks: float = 3.0
    block_interval_stop_s: float = 4.0  # kill-switch 閾値（移動中央値）


@dataclass
class LatencyCfg:
    """〔このクラスがすること〕 レイテンシ・鮮度の設定を保持します。"""

    ingest_ms: int = 10
    order_rt_ms: int = 60
    max_staleness_ms: int = 300


@dataclass
class VRLGConfig:
    """〔このクラスがすること〕 VRLG の設定ルート（サブセクションを内包）です。"""

    symbol: SymbolCfg = SymbolCfg()
    signal: SignalCfg = SignalCfg()
    exec: ExecCfg = ExecCfg()
    risk: RiskCfg = RiskCfg()
    latency: LatencyCfg = LatencyCfg()


# ───────────── ヘルパー（dict/属性どちらでも取り出せるように） ─────────────


def _sec(raw: Any, name: str) -> Any:
    """〔この関数がすること〕 セクション（symbol/signal/exec/risk/latency）を安全に取り出します。"""

    try:
        if isinstance(raw, Mapping):
            return raw.get(name, {})  # type: ignore[return-value]
        return getattr(raw, name)
    except Exception:
        return {}


def _val(sec: Any, key: str, default: Any) -> Any:
    """〔この関数がすること〕 値を dict/属性両対応で安全に取得します。"""

    try:
        if isinstance(sec, Mapping) and key in sec:  # type: ignore[arg-type]
            return sec[key]  # type: ignore[index]
        v = getattr(sec, key)
        return v
    except Exception:
        return default


# ───────────── 入口関数 ─────────────


def coerce_vrlg_config(data: Any) -> VRLGConfig:
    """〔この関数がすること〕 dict 等の生設定を VRLGConfig（属性アクセス可）へ変換します。"""

    s = _sec(data, "symbol")
    g = _sec(data, "signal")
    e = _sec(data, "exec")
    r = _sec(data, "risk")
    latency_section = _sec(data, "latency")

    symbol = SymbolCfg(
        name=str(_val(s, "name", SymbolCfg.name)),
        tick_size=float(_val(s, "tick_size", SymbolCfg.tick_size)),
    )
    signal = SignalCfg(
        N=int(_val(g, "N", SignalCfg.N)),
        x=float(_val(g, "x", SignalCfg.x)),
        y=float(_val(g, "y", SignalCfg.y)),
        z=float(_val(g, "z", SignalCfg.z)),
        obi_limit=float(_val(g, "obi_limit", SignalCfg.obi_limit)),
        T_roll=float(_val(g, "T_roll", SignalCfg.T_roll)),
        p_thresh=float(_val(g, "p_thresh", SignalCfg.p_thresh)),
        period_min_s=float(_val(g, "period_min_s", SignalCfg.period_min_s)),
        period_max_s=float(_val(g, "period_max_s", SignalCfg.period_max_s)),
        min_boundary_samples=int(_val(g, "min_boundary_samples", SignalCfg.min_boundary_samples)),
        min_off_samples=int(_val(g, "min_off_samples", SignalCfg.min_off_samples)),
    )
    exec_ = ExecCfg(
        order_ttl_ms=int(_val(e, "order_ttl_ms", ExecCfg.order_ttl_ms)),
        display_ratio=float(_val(e, "display_ratio", ExecCfg.display_ratio)),
        min_display_btc=float(_val(e, "min_display_btc", ExecCfg.min_display_btc)),
        max_exposure_btc=float(_val(e, "max_exposure_btc", ExecCfg.max_exposure_btc)),
        cooldown_factor=float(_val(e, "cooldown_factor", ExecCfg.cooldown_factor)),
        offset_ticks_normal=float(_val(e, "offset_ticks_normal", ExecCfg.offset_ticks_normal)),
        offset_ticks_deep=float(_val(e, "offset_ticks_deep", ExecCfg.offset_ticks_deep)),
        spread_collapse_ticks=float(_val(e, "spread_collapse_ticks", ExecCfg.spread_collapse_ticks)),
        side_mode=str(_val(e, "side_mode", ExecCfg.side_mode)).lower(),
        splits=int(_val(e, "splits", ExecCfg.splits)),
        percent_min=float(_val(e, "percent_min", ExecCfg.percent_min)),
        percent_max=float(_val(e, "percent_max", ExecCfg.percent_max)),
        min_clip_btc=float(_val(e, "min_clip_btc", ExecCfg.min_clip_btc)),
        equity_usd=float(_val(e, "equity_usd", ExecCfg.equity_usd)),
    )
    risk = RiskCfg(
        max_slippage_ticks=float(_val(r, "max_slippage_ticks", RiskCfg.max_slippage_ticks)),
        max_book_impact=float(_val(r, "max_book_impact", RiskCfg.max_book_impact)),
        time_stop_ms=int(_val(r, "time_stop_ms", RiskCfg.time_stop_ms)),
        stop_ticks=float(_val(r, "stop_ticks", RiskCfg.stop_ticks)),
        block_interval_stop_s=float(_val(r, "block_interval_stop_s", RiskCfg.block_interval_stop_s)),
    )
    latency = LatencyCfg(
        ingest_ms=int(_val(latency_section, "ingest_ms", LatencyCfg.ingest_ms)),
        order_rt_ms=int(_val(latency_section, "order_rt_ms", LatencyCfg.order_rt_ms)),
        max_staleness_ms=int(_val(latency_section, "max_staleness_ms", LatencyCfg.max_staleness_ms)),
    )
    return VRLGConfig(symbol=symbol, signal=signal, exec=exec_, risk=risk, latency=latency)


def load_vrlg_config(path: str) -> VRLGConfig:
    """〔この関数がすること〕 ファイルから設定を読み込み、VRLGConfig に変換します。"""

    try:
        from hl_core.utils.config import load_config  # 共通ローダ（YAML/TOML）
        raw = load_config(path)
    except Exception:
        # 軽量 TOML フォールバック
        try:
            import tomllib  # py3.11+
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except Exception:
            import tomli  # type: ignore
            with open(path, "rb") as f:
                raw = tomli.load(f)
    return coerce_vrlg_config(raw)
