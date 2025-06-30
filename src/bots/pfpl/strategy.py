# src/bots/pfpl/strategy.py
from __future__ import annotations

import logging
from typing import Any

from hl_core.utils.logger import setup_logger

setup_logger(bot_name="pfpl")  # ← Bot 切替時はここだけ変える

logger = logging.getLogger(__name__)


class PFPLStrategy:
    """
    Price-Feed Price Lag (PFPL) ボットの最小骨格。

    - on_depth_update / on_tick は後で実装
    - config (dict) を受け取り、パラメータを保持
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.mids: dict[str, str] = {}  # ★ 追加：最新ミッドを保持
        logger.info("PFPLStrategy initialised with %s", config)

    async def on_depth_update(self, depth: dict[str, Any]) -> None:  # noqa: D401
        """OrderBook 更新時に呼ばれるコールバック（後で実装）"""
        pass

    async def on_tick(self, tick: dict[str, Any]) -> None:  # noqa: D401
        """約定ティック更新時に呼ばれるコールバック（後で実装）"""
        pass

    # ───────────────────────── WS 受信フック ──────────────────────
    def on_message(self, data: dict[str, Any]) -> None:
        """
        WSClient から渡されるメッセージを処理するフック。
        ここでは `allMids` チャンネルの @1（BTC?）ミッドだけログに出す。
        """
        if data.get("channel") == "allMids":
            mids = data["data"]["mids"]
            self.mids.update(mids)
            mid1 = mids.get("@1")
            logger.info("on_message mid@1=%s", mid1)
            self.evaluate()

    # ───────────────────────── シグナル判定 ──────────────────────
    def evaluate(self) -> None:
        """
        最新 mid@1 が設定スプレッド閾値を超えたらシグナルを出すだけの雛形。
        今はログに出すだけ。後で Order API を呼ぶ処理に置き換える。
        """
        mid = self.mids.get("@1")
        if mid is None:
            return  # まだデータが無い

        spread = float(mid) - float(self.config.get("target_mid", 30.0))
        threshold = self.config.get("spread_threshold", 0.5)
        if abs(spread) >= threshold:
            logger.info("EVALUATE: spread=%.4f → SIGNAL!", spread)
