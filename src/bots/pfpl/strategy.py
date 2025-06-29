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
        logger.info("PFPLStrategy initialised with %s", config)

    async def on_depth_update(self, depth: dict[str, Any]) -> None:  # noqa: D401
        """OrderBook 更新時に呼ばれるコールバック（後で実装）"""
        pass

    async def on_tick(self, tick: dict[str, Any]) -> None:  # noqa: D401
        """約定ティック更新時に呼ばれるコールバック（後で実装）"""
        pass
