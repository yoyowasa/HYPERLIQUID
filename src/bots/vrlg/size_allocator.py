# 〔このモジュールがすること〕
# 口座残高（USD）と設定（percent_min/percent_max/min_clip_btc/max_exposure_btc）から、
# VRLG の 1 クリップ（子注文の親となる“クリップ総量”）サイズ（BTC）を決定します。
# RiskManager から渡される size_multiplier を掛け、最終的な BTC 数量を返します。

from __future__ import annotations

from typing import Any

import logging

logger = logging.getLogger("bots.vrlg.size")


class SizeAllocator:
    """〔このクラスがすること〕
    設定と口座残高に基づいて「次に出すクリップサイズ（BTC）」を算出します。
    """

    def __init__(self, cfg: Any) -> None:
        """〔このメソッドがすること〕 設定値を読み込み、既定の口座残高USDを保持します。"""
        self.cfg = cfg
        ex = getattr(cfg, "exec", {})
        self.percent_min = float(getattr(ex, "percent_min", 0.002))   # 0.2%
        self.percent_max = float(getattr(ex, "percent_max", 0.005))   # 0.5%
        self.min_clip_btc = float(getattr(ex, "min_clip_btc", 0.001))
        self.max_exposure_btc = float(getattr(ex, "max_exposure_btc", 0.8))
        self._equity_usd = float(getattr(ex, "equity_usd", 10_000.0))

    def update_equity_usd(self, equity_usd: float) -> None:
        """〔このメソッドがすること〕 外部（口座API等）から最新の口座残高USDを注入します。"""
        try:
            self._equity_usd = max(0.0, float(equity_usd))
        except Exception as e:
            logger.debug("equity update failed: %s", e)

    def next_size(self, mid: float, risk_mult: float = 1.0) -> float:
        """〔このメソッドがすること〕
        クリップ総量（BTC）を返します。手順:
          1) pct = clamp(percent_max * risk_mult, lower=percent_min, upper=percent_max)
          2) notional_usd = equity_usd * pct
          3) size_btc = notional_usd / mid
          4) size_btc < min_clip_btc → 0 を返す（発注スキップ）
          5) size_btc を max_exposure_btc で上限クランプ
        """
        try:
            midf = float(mid)
            if midf <= 0.0 or self._equity_usd <= 0.0:
                return 0.0

            # 1) パーセンテージ（Risk で 0.5 などに縮む想定）
            pct = self.percent_max * float(risk_mult)
            if pct < self.percent_min:
                pct = self.percent_min
            if pct > self.percent_max:
                pct = self.percent_max

            # 2) USD → 3) BTC
            notional = self._equity_usd * pct
            size_btc = notional / midf

            # 4) 取引所の最小発注仕様などに配慮して、閾値未満はスキップ
            if size_btc < self.min_clip_btc:
                return 0.0

            # 5) 露出上限でクランプ（最終安全弁。実際の露出管理は ExecutionEngine 側でも実施）
            size_btc = min(size_btc, self.max_exposure_btc)

            # 数値安定のため軽く丸め
            return float(round(size_btc, 6))
        except Exception as e:
            logger.debug("size allocation failed: %s", e)
            return 0.0
