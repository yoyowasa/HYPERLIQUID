# 〔このモジュールがすること〕
# VRLG の設定スキーマ（symbol/signal/exec/risk/latency）を「型つき」で定義し、
# 辞書や属性ベースの生設定から VRLGConfig に変換（coerce）します。
# pydantic があれば BaseModel を使用、無ければ dataclass で代替します。

from __future__ import annotations

from typing import Any, Mapping

# ─────────────── 可能なら pydantic、無ければ dataclass にフォールバック ───────────────
try:
    from pydantic import BaseModel  # type: ignore

    class _Base(BaseModel):
        """〔この基底クラスがすること〕 pydantic モデルの共通基底（fallback 時は未使用）。"""

        pass

    _USE_PYDANTIC = True
except Exception:  # pragma: no cover
    from dataclasses import dataclass as _dataclass

    class _Base:  # type: ignore
        """〔この基底クラスがすること〕 dataclass フォールバック時のダミー基底。"""

        pass

    def _dc(cls):  # decorator alias
        return _dataclass(frozen=False)(cls)

    _USE_PYDANTIC = False


def _get_section(raw: Any, name: str) -> Mapping[str, Any]:
    """〔この関数がすること〕
    生設定（dict または属性オブジェクト）からセクション名 name を辞書として取り出します。
    無ければ空辞書を返します。
    """

    try:
        sec = getattr(raw, name)
        if isinstance(sec, Mapping):
            return sec  # type: ignore[return-value]
        # 属性オブジェクトを dict っぽく変換
        return {k: getattr(sec, k) for k in dir(sec) if not k.startswith("__")}
    except Exception:
        try:
            return raw.get(name, {})  # type: ignore[union-attr]
        except Exception:
            return {}


# ─────────────── セクション定義（pydantic か dataclass） ───────────────

if _USE_PYDANTIC:

    class SymbolCfg(_Base):
        """〔このクラスがすること〕 銘柄とティックサイズの設定を表します。"""

        name: str = "BTCUSD-PERP"
        tick_size: float = 0.5

    class SignalCfg(_Base):
        """〔このクラスがすること〕 位相検出・4条件ゲートのパラメータを表します。"""

        N: int = 80
        x: float = 0.25
        y: float = 2.0
        z: float = 0.6
        obi_limit: float = 0.6
        T_roll: float = 30.0

    class ExecCfg(_Base):
        """〔このクラスがすること〕 発注ロジック（TTL/アイスバーグ/クールダウン）を表します。"""

        order_ttl_ms: int = 1000
        display_ratio: float = 0.25
        min_display_btc: float = 0.01
        max_exposure_btc: float = 0.8
        cooldown_factor: float = 2.0

    class RiskCfg(_Base):
        """〔このクラスがすること〕 ルールベースのリスク閾値を表します。"""

        max_slippage_ticks: float = 1.0
        max_book_impact: float = 0.02
        time_stop_ms: int = 1200
        stop_ticks: float = 3.0

    class LatencyCfg(_Base):
        """〔このクラスがすること〕 レイテンシ想定（主にメトリクス/BT用）を表します。"""

        ingest_ms: int = 10
        order_rt_ms: int = 60

    class VRLGConfig(_Base):
        """〔このクラスがすること〕 VRLG のトップレベル設定（全セクションを内包）を表します。"""

        symbol: SymbolCfg = SymbolCfg()
        signal: SignalCfg = SignalCfg()
        exec: ExecCfg = ExecCfg()
        risk: RiskCfg = RiskCfg()
        latency: LatencyCfg = LatencyCfg()

else:

    @_dc
    class SymbolCfg(_Base):  # type: ignore[misc]
        """〔このクラスがすること〕 銘柄とティックサイズの設定を表します。"""

        name: str = "BTCUSD-PERP"
        tick_size: float = 0.5

    @_dc
    class SignalCfg(_Base):  # type: ignore[misc]
        """〔このクラスがすること〕 位相検出・4条件ゲートのパラメータを表します。"""

        N: int = 80
        x: float = 0.25
        y: float = 2.0
        z: float = 0.6
        obi_limit: float = 0.6
        T_roll: float = 30.0

    @_dc
    class ExecCfg(_Base):  # type: ignore[misc]
        """〔このクラスがすること〕 発注ロジック（TTL/アイスバーグ/クールダウン）を表します。"""

        order_ttl_ms: int = 1000
        display_ratio: float = 0.25
        min_display_btc: float = 0.01
        max_exposure_btc: float = 0.8
        cooldown_factor: float = 2.0

    @_dc
    class RiskCfg(_Base):  # type: ignore[misc]
        """〔このクラスがすること〕 ルールベースのリスク閾値を表します。"""

        max_slippage_ticks: float = 1.0
        max_book_impact: float = 0.02
        time_stop_ms: int = 1200
        stop_ticks: float = 3.0

    @_dc
    class LatencyCfg(_Base):  # type: ignore[misc]
        """〔このクラスがすること〕 レイテンシ想定（主にメトリクス/BT用）を表します。"""

        ingest_ms: int = 10
        order_rt_ms: int = 60

    @_dc
    class VRLGConfig(_Base):  # type: ignore[misc]
        """〔このクラスがすること〕 VRLG のトップレベル設定（全セクションを内包）を表します。"""

        symbol: SymbolCfg = SymbolCfg()
        signal: SignalCfg = SignalCfg()
        exec: ExecCfg = ExecCfg()
        risk: RiskCfg = RiskCfg()
        latency: LatencyCfg = LatencyCfg()


# ─────────────── 変換ユーティリティ ───────────────


def coerce_vrlg_config(raw: Any) -> VRLGConfig:
    """〔この関数がすること〕
    dict や属性オブジェクトなど「生の設定」から VRLGConfig へ安全に変換します。
    足りないキーは既定値で補います。
    """

    # すでに VRLGConfig ならそのまま返す
    if isinstance(raw, VRLGConfig):
        return raw

    sec_symbol = _get_section(raw, "symbol")
    sec_signal = _get_section(raw, "signal")
    sec_exec = _get_section(raw, "exec")
    sec_risk = _get_section(raw, "risk")
    sec_latency = _get_section(raw, "latency")

    symbol = SymbolCfg(**{  # type: ignore[arg-type]
        "name": sec_symbol.get("name", "BTCUSD-PERP"),  # type: ignore[union-attr]
        "tick_size": float(sec_symbol.get("tick_size", 0.5)),  # type: ignore[union-attr]
    })
    signal = SignalCfg(**{
        "N": int(sec_signal.get("N", 80)),
        "x": float(sec_signal.get("x", 0.25)),
        "y": float(sec_signal.get("y", 2.0)),
        "z": float(sec_signal.get("z", 0.6)),
        "obi_limit": float(sec_signal.get("obi_limit", 0.6)),
        "T_roll": float(sec_signal.get("T_roll", 30.0)),
    })
    exec_ = ExecCfg(**{
        "order_ttl_ms": int(sec_exec.get("order_ttl_ms", 1000)),
        "display_ratio": float(sec_exec.get("display_ratio", 0.25)),
        "min_display_btc": float(sec_exec.get("min_display_btc", 0.01)),
        "max_exposure_btc": float(sec_exec.get("max_exposure_btc", 0.8)),
        "cooldown_factor": float(sec_exec.get("cooldown_factor", 2.0)),
    })
    risk = RiskCfg(**{
        "max_slippage_ticks": float(sec_risk.get("max_slippage_ticks", 1.0)),
        "max_book_impact": float(sec_risk.get("max_book_impact", 0.02)),
        "time_stop_ms": int(sec_risk.get("time_stop_ms", 1200)),
        "stop_ticks": float(sec_risk.get("stop_ticks", 3.0)),
    })
    latency = LatencyCfg(**{
        "ingest_ms": int(sec_latency.get("ingest_ms", 10)),
        "order_rt_ms": int(sec_latency.get("order_rt_ms", 60)),
    })

    return VRLGConfig(symbol=symbol, signal=signal, exec=exec_, risk=risk, latency=latency)  # type: ignore[call-arg]


def vrlg_config_to_dict(cfg: VRLGConfig) -> dict[str, Any]:
    """〔この関数がすること〕 VRLGConfig を辞書に変換します（ロギング/保存に利用できます）。"""

    if _USE_PYDANTIC and hasattr(cfg, "model_dump"):
        return cfg.model_dump()  # type: ignore[return-value]
    # dataclass フォールバック
    return {
        "symbol": vars(cfg.symbol),
        "signal": vars(cfg.signal),
        "exec": vars(cfg.exec),
        "risk": vars(cfg.risk),
        "latency": vars(cfg.latency),
    }

