# 〔このモジュールがすること〕
# 口座残高（USD想定）に対して 0.2–0.5% を基準に「1クリップの発注量（BTC）」を決めます。
# - PFPL など他Botでも再利用可能な独立ユニット
# - APIが無い環境でも動くよう equity_usd は設定値/既定値から取得（後でAPIに差し替え可能）

from __future__ import annotations

from typing import Any


def _get(cfg: Any, section: str, key: str, default):
    """〔この関数がすること〕 設定（dict/属性どちらでもOK）から値を安全に取り出します。"""
    try:
        sec = getattr(cfg, section)
        return getattr(sec, key, default)
    except Exception:
        try:
            return cfg[section].get(key, default)  # type: ignore[index]
        except Exception:
            return default


class SizeAllocator:
    """〔このクラスがすること〕
    1トレード（クリップ）あたりのサイズ（BTC）を決めます。
    - percent_min / percent_max: 口座残高に対する割合（0.002=0.2% 〜 0.005=0.5%）
    - splits: クリップ分割数（>1 のとき、算出サイズを均等割り）
    - max_exposure_btc / min_clip_btc で上下限をクランプ
    """

    def __init__(self, cfg) -> None:
        """〔このメソッドがすること〕 各パラメータを設定から読み込み、既定値を用意します。"""
        self.percent_min: float = float(_get(cfg, "exec", "percent_min", 0.002))  # 0.2%
        self.percent_max: float = float(_get(cfg, "exec", "percent_max", 0.005))  # 0.5%
        self.splits: int = int(_get(cfg, "exec", "splits", 1))
        self.min_clip_btc: float = float(_get(cfg, "exec", "min_clip_btc", 0.001))
        self.max_exposure_btc: float = float(_get(cfg, "exec", "max_exposure_btc", 0.8))
        # equity_usd は将来 API で差し替え可能。いまは設定 or 既定値で運用
        self.equity_usd: float = float(
            _get(cfg, "exec", "equity_usd", _get(cfg, "risk", "equity_usd", 10000.0))
        )

    def next_size(self, mid: float, risk_mult: float = 1.0) -> float:
        """〔このメソッドがすること〕
        現在のミッド価格（USD）とリスク倍率から、1クリップの BTC 数量を返します。
        - 中心割合 = (percent_min + percent_max)/2
        - 口座残高×中心割合×risk_mult を USD→BTC に換算
        - splits>1 のとき均等割り
        - min_clip_btc〜max_exposure_btc にクランプ
        """
        if mid <= 0 or self.equity_usd <= 0:
            return 0.0
        base_pct = 0.5 * (self.percent_min + self.percent_max)
        usd = self.equity_usd * base_pct * max(risk_mult, 0.0)
        btc = usd / mid
        if self.splits > 1:
            btc /= float(self.splits)
        # クリップの下限/上限を適用
        btc = max(self.min_clip_btc, min(btc, self.max_exposure_btc))
        return max(0.0, btc)
