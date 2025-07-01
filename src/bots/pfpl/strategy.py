# src/bots/pfpl/strategy.py
from __future__ import annotations
import os
import logging
from typing import Any
import asyncio
import hmac, hashlib, json
from hl_core.api import HTTPClient
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
        self.mids: dict[str, str] = {}
        # ★ 環境変数から公開アドレスと秘密鍵を取得
        self.account = os.getenv("HL_ACCOUNT_ADDR")
        self.secret  = os.getenv("HL_API_SECRET")
        if not (self.account and self.secret):
            raise RuntimeError("HL_ACCOUNT_ADDR / HL_API_SECRET が未設定")
        self.http = HTTPClient(base_url="https://api.hyperliquid.xyz", api_key=api_key)
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
        mid = self.mids.get("@1")
        if mid is None:
            return
        spread = float(mid) - float(self.config.get("target_mid", 30.0))
        threshold = self.config.get("spread_threshold", 0.5)
        if abs(spread) >= threshold:
            side = "BUY" if spread < 0 else "SELL"
            asyncio.create_task(self.place_order(side, 0.01))  # ★追加

    # ─────────────── 注文 ここを実装 ────────────────
    # ─── 署名付きで実注文 (テストネット推奨) ───
    async def place_order(self, side: str, size: float) -> None:
        order = {
            "market": "@1",            # BTC/USDC の例
            "type": "market",
            "side": side.lower(),      # "buy" / "sell"
            "size": size,
            "account": self.account,
        }
        order["signature"] = self._sign(order)

        try:
            resp = await self.http.post("exchange", order)
            logger.info("ORDER OK: %s", resp)
        except Exception as e:  # noqa: BLE001
            logger.error("ORDER FAIL: %s", e)

    def _sign(self, payload: dict[str, Any]) -> str:
        """API Wallet Secret で HMAC-SHA256 署名（例）"""
        msg = json.dumps(payload, separators=(",", ":")).encode()
        return hmac.new(self.secret.encode(), msg, hashlib.sha256).hexdigest()
