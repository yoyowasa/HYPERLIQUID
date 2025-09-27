# 〔このモジュールがすること〕
# VRLG の設定スキーマ（symbol/signal/exec/risk/latency）を「型つき」で定義し、
# 辞書や属性ベースの生設定から VRLGConfig に変換（coerce）します。
# pydantic があれば BaseModel を使用、無ければ dataclass で代替します。

from __future__ import annotations

from typing import Any, Callable, Mapping, TypeVar, cast

T = TypeVar("T")

# ─────────────── 可能なら pydantic、無ければ dataclass にフォールバック ───────────────
try:  # pragma: no cover - optional dependency
    from pydantic import BaseModel as _ImportedBaseModel  # type: ignore
except Exception:  # pragma: no cover - runtime fallback
    _ImportedBaseModel = None

_PydanticBaseModel: type[Any] | None = cast("type[Any] | None", _ImportedBaseModel)

_USE_PYDANTIC = _PydanticBaseModel is not None

_BaseConfig: type[Any]

if _USE_PYDANTIC:

    class _PydanticBase(_PydanticBaseModel):
        """〔この基底クラスがすること〕 pydantic モデルの共通基底。"""

        pass

    _BaseConfig = _PydanticBase

    def _decorate_impl_pydantic(cls: type[Any]) -> type[Any]:
        return cls

    def _section_default_impl_pydantic(factory: Callable[[], Any]) -> Any:
        return factory()

    _decorate_impl = _decorate_impl_pydantic
    _section_default_impl = _section_default_impl_pydantic

else:
    from dataclasses import dataclass as _dataclass, field as _dc_field

    class _DataclassBase:
        """〔この基底クラスがすること〕 dataclass フォールバック時のダミー基底。"""

        pass

    _BaseConfig = _DataclassBase

    def _decorate_impl_dataclass(cls: type[Any]) -> type[Any]:
        return _dataclass(frozen=False)(cls)

    def _section_default_impl_dataclass(factory: Callable[[], Any]) -> Any:
        return _dc_field(default_factory=factory)

    _decorate_impl = _decorate_impl_dataclass
    _section_default_impl = _section_default_impl_dataclass


def _decorate(cls: type[T]) -> type[T]:
    return cast(type[T], _decorate_impl(cls))


def _section_default(factory: Callable[[], T]) -> Any:
    return _section_default_impl(factory)


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

@_decorate
class SymbolCfg(_BaseConfig):
    """〔このクラスがすること〕 銘柄とティックサイズの設定を表します。"""

    name: str = "BTCUSD-PERP"
    tick_size: float = 0.5


@_decorate
class SignalCfg(_BaseConfig):
    """〔このクラスがすること〕 位相検出・4条件ゲートのパラメータを表します。"""

    N: int = 80
    x: float = 0.25
    y: float = 2.0
    z: float = 0.6
    obi_limit: float = 0.6
    T_roll: float = 30.0


@_decorate
class ExecCfg(_BaseConfig):
    """〔このクラスがすること〕 発注ロジック（TTL/アイスバーグ/クールダウン）を表します。"""

    order_ttl_ms: int = 1000
    display_ratio: float = 0.25
    min_display_btc: float = 0.01
    max_exposure_btc: float = 0.8
    cooldown_factor: float = 2.0
    side_mode: str = "both"        # 〔このフィールドがすること〕 発注の向き: "both" | "buy" | "sell"
    offset_ticks_normal: float = 0.5   # 〔このフィールドがすること〕 通常置きの価格オフセット（±tick）
    offset_ticks_deep: float = 1.5     # 〔このフィールドがすること〕 深置きの価格オフセット（±tick）
    spread_collapse_ticks: float = 1.0 # 〔このフィールドがすること〕 早期IOCの縮小判定のしきい値（tick）
    percent_min: float = 0.002     # 〔このフィールドがすること〕 口座残高に対する最小割合（0.2%）
    percent_max: float = 0.005     # 〔このフィールドがすること〕 口座残高に対する最大割合（0.5%）
    splits: int = 1                # 〔このフィールドがすること〕 クリップ分割数（>1 で均等割り）
    min_clip_btc: float = 0.001    # 〔このフィールドがすること〕 1クリップの最小BTC数量
    equity_usd: float = 10000.0    # 〔このフィールドがすること〕 口座残高USD（API未接続時の既定値）


@_decorate
class RiskCfg(_BaseConfig):
    """〔このクラスがすること〕 ルールベースのリスク閾値を表します。"""

    max_slippage_ticks: float = 1.0
    max_book_impact: float = 0.02
    time_stop_ms: int = 1200
    stop_ticks: float = 3.0


@_decorate
class LatencyCfg(_BaseConfig):
    """〔このクラスがすること〕 レイテンシ想定（主にメトリクス/BT用）を表します。"""

    ingest_ms: int = 10
    order_rt_ms: int = 60
    max_staleness_ms: int = 300  # 〔このフィールドがすること〕 この ms を超えて古い特徴量は発注ロジックから除外


@_decorate
class VRLGConfig(_BaseConfig):
    """〔このクラスがすること〕 VRLG のトップレベル設定（全セクションを内包）を表します。"""

    symbol: SymbolCfg = _section_default(SymbolCfg)
    signal: SignalCfg = _section_default(SignalCfg)
    exec: ExecCfg = _section_default(ExecCfg)
    risk: RiskCfg = _section_default(RiskCfg)
    latency: LatencyCfg = _section_default(LatencyCfg)


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

    symbol = SymbolCfg(**{
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
        "side_mode": str(sec_exec.get("side_mode", "both")),
        "offset_ticks_normal": float(sec_exec.get("offset_ticks_normal", 0.5)),
        "offset_ticks_deep": float(sec_exec.get("offset_ticks_deep", 1.5)),
        "spread_collapse_ticks": float(sec_exec.get("spread_collapse_ticks", 1.0)),
        "percent_min": float(sec_exec.get("percent_min", 0.002)),
        "percent_max": float(sec_exec.get("percent_max", 0.005)),
        "splits": int(sec_exec.get("splits", 1)),
        "min_clip_btc": float(sec_exec.get("min_clip_btc", 0.001)),
        "equity_usd": float(sec_exec.get("equity_usd", 10000.0)),
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
        "max_staleness_ms": int(sec_latency.get("max_staleness_ms", 300)),
    })

    return VRLGConfig(symbol=symbol, signal=signal, exec=exec_, risk=risk, latency=latency)


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

