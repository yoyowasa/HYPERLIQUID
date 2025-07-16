from __future__ import annotations
import csv
from datetime import datetime
from pathlib import Path
from typing import Final


class AnalysisLogger:
    """戦略評価用 CSV ロガー（ENTRY/EXIT 1 行ずつ追記）"""

    HEADERS: Final[list[str]] = [
        "ts_iso",
        "unix_ms",
        "symbol",
        "side",
        "size",
        "price",
        "reason",
    ]

    def __init__(self, csv_path: str | Path) -> None:
        self.csv_path = Path(csv_path)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.csv_path.exists():
            with self.csv_path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.HEADERS)

    def log_trade(
        self,
        *,
        symbol: str,
        side: str,
        size: float,
        price: float,
        reason: str,
        ts: datetime | None = None,
    ) -> None:
        ts = ts or datetime.utcnow()
        row = [
            ts.isoformat(timespec="seconds") + "Z",
            int(ts.timestamp() * 1000),
            symbol,
            side,
            f"{size:.8f}",
            f"{price:.2f}",
            reason,
        ]
        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
