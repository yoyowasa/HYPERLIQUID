from __future__ import annotations
import csv
from datetime import datetime
from pathlib import Path
from typing import Final
import time


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
        "notional_usd",
        "pos_id",  # 追加: オープンとクローズを紐付ける ID
        "pnl_usd",  # 追加: クローズ時だけ数値、オープン時は空
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
        unix_ms = int(time.time() * 1000)
        ts = ts or datetime.utcnow()

        notional_usd = float(size) * float(price)
        trade_id = str(unix_ms)  # 例: 1752921064524_buy

        row = [
            ts.isoformat(timespec="seconds") + "Z",
            unix_ms,
            symbol,
            side.upper(),
            f"{size:.8f}",
            f"{price:.2f}",
            reason,
            notional_usd,  # ← notional_usd を使用
            trade_id,  # pos_id
            "",  # pnl_usd（オープン時は空）
        ]

        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            # --- クローズ判定 & PnL 追記 ---------------------------------
            tmp_lines = self.csv_path.read_text(encoding="utf-8").splitlines()

            # 直近行（末尾）をチェックして “反対売買ならクローズ” とみなす
            if len(tmp_lines) > 1:
                cols = tmp_lines[-1].split(",")
                last_symbol, last_side = cols[2], cols[3]

                # 同じ銘柄で方向が反対ならペアリング成立
                if last_symbol == symbol and last_side != side:
                    open_px = float(cols[5])  # 5: price
                    size_f = float(size)

                    if last_side == "BUY":  # BUY → SELL
                        pnl = (price - open_px) * size_f
                    else:  # SELL → BUY
                        pnl = (open_px - price) * size_f

                    cols[9] = f"{pnl:.4f}"  # 9: pnl_usd
                    tmp_lines[-1] = ",".join(cols)
                    self.csv_path.write_text(
                        "\n".join(tmp_lines) + "\n", encoding="utf-8"
                    )
                    return  # クローズ完了したので終了

            # ここに来たらオープン行として追記
            csv.writer(f).writerow(row)
