
# 〔このモジュールがすること〕
# VRLG の意思決定イベントを「1行JSON」で記録します（リングバッファ + 任意でファイル書き出し）。
# ログ項目例: signal/order_intent/order_submitted/exit/risk_pause/fill/block_interval など。

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from hl_core.utils.logger import get_logger

logger = get_logger("DecisionLog")


class DecisionLogger:
    """〔このクラスがすること〕
    意思決定イベントをリングバッファに蓄え、必要なら JSONL ファイルへ追記します。
    """

    def __init__(self, maxlen: int = 10000, filepath: Optional[str] = None) -> None:
        """〔このメソッドがすること〕 バッファ長と出力先（任意）を設定します。"""
        self._buf: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._path: Optional[str] = filepath

    def log(self, event: str, **fields: Any) -> None:
        """〔このメソッドがすること〕
        任意のキー/値を受け取り、{t,event,...} 形式で記録します。ファイル指定があれば追記します。
        """
        rec: Dict[str, Any] = {"t": time.time(), "event": str(event)}
        rec.update(fields)
        self._buf.append(rec)
        if self._path:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception as e:  # pragma: no cover
                logger.debug("decision log write failed: %s", e)

    def latest(self, n: int = 100) -> List[Dict[str, Any]]:
        """〔このメソッドがすること〕 直近 n 件の記録を返します（デバッグ用）。"""
        if n <= 0:
            return []
        return list(self._buf)[-n:]
