from __future__ import annotations

import argparse
import asyncio
import json
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from hl_core.utils.logger import get_logger

logger = get_logger("VRLG.recorder")

# ─────────────────────────── 出力ライタ（Parquet or JSONL） ───────────────────────────

try:
    import pyarrow as pa  # type: ignore
    import pyarrow.parquet as pq  # type: ignore

    _HAVE_PARQUET = True
except Exception:  # pragma: no cover
    _HAVE_PARQUET = False


@dataclass
class Row:
    """〔このデータクラスがすること〕 1 レコード（行）を表します。"""

    t: float
    payload: Dict[str, Any]


class RotatingWriter:
    """〔このクラスがすること〕
    ストリームごと（blocks / level2 / trades）の出力ファイルを管理します。
    - Parquet があれば Parquet で、無ければ JSONL で保存
    - rotate_sec ごとに新しいファイルを作成
    - flush_rows 件ごとにフラッシュ
    """

    def __init__(
        self,
        outdir: Path,
        prefix: str,
        rotate_sec: int,
        flush_rows: int,
        schema: Optional["pa.Schema"] = None,
    ) -> None:
        self.outdir = outdir
        self.prefix = prefix
        self.rotate_sec = int(rotate_sec)
        self.flush_rows = int(flush_rows)
        self.schema = schema if _HAVE_PARQUET else None

        self._current_path: Optional[Path] = None
        self._current_start: float = 0.0
        self._buffer: list[Dict[str, Any]] = []
        self._pq_writer: Optional["pq.ParquetWriter"] = None

    def _new_path(self) -> Path:
        ts = time.strftime("%Y%m%d_%H%M%S")
        ext = "parquet" if _HAVE_PARQUET else "jsonl"
        name = f"{self.prefix}-{ts}.{ext}"
        return self.outdir / name

    def _need_rotate(self) -> bool:
        if self._current_path is None:
            return True
        return (time.time() - self._current_start) >= self.rotate_sec

    def _open(self) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        self._current_path = self._new_path()
        self._current_start = time.time()
        self._buffer.clear()
        if _HAVE_PARQUET:
            # Parquet: スキーマが無ければ payload のキーから自動推定（最初の行で確定）します
            self._pq_writer = None  # 最初の write で open
        logger.info("open file: %s", self._current_path.name)

    def _close(self) -> None:
        if self._current_path is None:
            return
        self._flush(force=True)
        if self._pq_writer is not None:
            try:
                self._pq_writer.close()
            except Exception:
                pass
            self._pq_writer = None
        logger.info("close file: %s", self._current_path.name)
        self._current_path = None

    def _write_parquet_batch(self, rows: list[Dict[str, Any]]) -> None:
        assert _HAVE_PARQUET and self._current_path is not None
        # スキーマが無ければ推定して ParquetWriter を開く
        if self._pq_writer is None:
            if self.schema is None:
                # キー集合から「t: float64 + 各フィールド」を推定
                sample = rows[0]
                fields = [pa.field("t", pa.float64())]
                for k, v in sample.items():
                    if k == "t":
                        continue
                    pa_type = pa.float64()
                    if isinstance(v, (int,)):
                        pa_type = pa.int64()
                    elif isinstance(v, (str,)):
                        pa_type = pa.string()
                    fields.append(pa.field(k, pa_type))
                self.schema = pa.schema(fields)
            self._pq_writer = pq.ParquetWriter(self._current_path, self.schema)  # type: ignore[arg-type]

        table = pa.Table.from_pylist(rows, schema=self.schema)
        self._pq_writer.write_table(table)  # type: ignore[union-attr]

    def _write_jsonl_batch(self, rows: list[Dict[str, Any]]) -> None:
        assert self._current_path is not None
        with self._current_path.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _flush(self, force: bool = False) -> None:
        if not self._buffer:
            return
        if self._current_path is None:
            self._open()
        if _HAVE_PARQUET:
            self._write_parquet_batch(self._buffer)
        else:
            self._write_jsonl_batch(self._buffer)
        self._buffer.clear()
        if force:
            # ParquetWriter は内部で flush 済み、JSONL は明示 flush 済み
            pass

    def write(self, row: Row) -> None:
        """〔このメソッドがすること〕 1 レコードをバッファに積み、必要ならフラッシュ・ローテートします。"""

        if self._need_rotate():
            self._close()
            self._open()
        data = {"t": float(row.t), **row.payload}
        self._buffer.append(data)
        if len(self._buffer) >= self.flush_rows:
            self._flush()

    def close(self) -> None:
        """〔このメソッドがすること〕 ファイルを安全にクローズします。"""

        self._close()


# ─────────────────────────── WS 消費タスク（blocks / level2 / trades） ───────────────────────────


def _get(attr_or_dict: Any, key: str, default: Any = None) -> Any:
    """〔この関数がすること〕 dict/attr のどちらでも値を安全に取り出します。"""

    try:
        return getattr(attr_or_dict, key)
    except Exception:
        try:
            return attr_or_dict[key]  # type: ignore[index]
        except Exception:
            return default


async def consume_blocks(writer: RotatingWriter, stop: asyncio.Event) -> None:
    """〔この関数がすること〕 blocks WS を購読し、height/timestamp を保存します。"""

    try:
        from hl_core.api.ws import subscribe_blocks  # type: ignore
    except Exception as e:
        logger.warning("blocks WS adapter unavailable: %s; waiting…", e)
        await stop.wait()
        return

    async for msg in subscribe_blocks():
        now = time.time()
        row = Row(
            t=now,
            payload={
                "height": int(_get(msg, "height", -1)),
                "block_ts": float(_get(msg, "timestamp", now)),
            },
        )
        writer.write(row)
        if stop.is_set():
            break


async def consume_level2(symbol: str, writer: RotatingWriter, stop: asyncio.Event) -> None:
    """〔この関数がすること〕 level2 WS を購読し、最良気配（L1）を保存します。"""

    try:
        from hl_core.api.ws import subscribe_level2  # type: ignore
    except Exception as e:
        logger.warning("level2 WS adapter unavailable: %s; waiting…", e)
        await stop.wait()
        return

    async for book in subscribe_level2(symbol):
        row = Row(
            t=time.time(),
            payload={
                "best_bid": float(_get(book, "best_bid", 0.0)),
                "best_ask": float(_get(book, "best_ask", 0.0)),
                "bid_size_l1": float(_get(book, "bid_size_l1", 0.0)),
                "ask_size_l1": float(_get(book, "ask_size_l1", 0.0)),
            },
        )
        writer.write(row)
        if stop.is_set():
            break


async def consume_trades(symbol: str, writer: RotatingWriter, stop: asyncio.Event) -> None:
    """〔この関数がすること〕 trades WS を購読し、約定（価格/サイズ/サイド）を保存します。"""

    try:
        from hl_core.api.ws import subscribe_trades  # type: ignore
    except Exception as e:
        logger.warning("trades WS adapter unavailable: %s; waiting…", e)
        await stop.wait()
        return

    async for tr in subscribe_trades(symbol):
        row = Row(
            t=time.time(),
            payload={
                "price": float(_get(tr, "price", 0.0)),
                "size": float(_get(tr, "size", 0.0)),
                "side": str(_get(tr, "side", "")),
            },
        )
        writer.write(row)
        if stop.is_set():
            break


# ─────────────────────────── エントリポイント ───────────────────────────


def parse_args() -> argparse.Namespace:
    """〔この関数がすること〕 CLI 引数（シンボル/保存先/回転間隔など）を解釈します。"""

    p = argparse.ArgumentParser(description="Record Hyperliquid WS streams to Parquet/JSONL")
    p.add_argument("--symbol", default="BTCUSD-PERP", help="symbol to subscribe (for level2/trades)")
    p.add_argument("--outdir", type=Path, default=Path("data/recordings"), help="output directory")
    p.add_argument("--rotate-sec", type=int, default=900, help="file rotation interval seconds")
    p.add_argument("--flush-rows", type=int, default=200, help="flush per this many rows")
    p.add_argument("--include-trades", action="store_true", help="also record trades stream")
    return p.parse_args()


async def _run() -> int:
    """〔この関数がすること〕
    ライタを準備して WS タスクを並列起動、シグナルで停止し安全にクローズします。
    """

    args = parse_args()
    stop = asyncio.Event()

    # スキーマ（Parquet のときに適用; JSONL は不要）
    if _HAVE_PARQUET:
        import pyarrow as pa  # type: ignore

        schema_blocks = pa.schema(
            [pa.field("t", pa.float64()), pa.field("height", pa.int64()), pa.field("block_ts", pa.float64())]
        )
        schema_l2 = pa.schema(
            [
                pa.field("t", pa.float64()),
                pa.field("best_bid", pa.float64()),
                pa.field("best_ask", pa.float64()),
                pa.field("bid_size_l1", pa.float64()),
                pa.field("ask_size_l1", pa.float64()),
            ]
        )
        schema_trades = pa.schema(
            [
                pa.field("t", pa.float64()),
                pa.field("price", pa.float64()),
                pa.field("size", pa.float64()),
                pa.field("side", pa.string()),
            ]
        )
    else:
        schema_blocks = schema_l2 = schema_trades = None  # type: ignore[assignment]

    w_blocks = RotatingWriter(args.outdir, "blocks", args.rotate_sec, args.flush_rows, schema_blocks)
    w_l2 = RotatingWriter(args.outdir, "level2", args.rotate_sec, args.flush_rows, schema_l2)
    w_trades = RotatingWriter(args.outdir, "trades", args.rotate_sec, args.flush_rows, schema_trades)

    tasks = [
        asyncio.create_task(consume_blocks(w_blocks, stop), name="rec.blocks"),
        asyncio.create_task(consume_level2(args.symbol, w_l2, stop), name="rec.level2"),
    ]
    if args.include_trades:
        tasks.append(asyncio.create_task(consume_trades(args.symbol, w_trades, stop), name="rec.trades"))

    loop = asyncio.get_running_loop()

    def _sig_handler(*_: object) -> None:
        stop.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _sig_handler)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass

    # 実行
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # ファイルを安全にクローズ
        for w in (w_blocks, w_l2, w_trades):
            try:
                w.close()
            except Exception:
                pass
    return 0


def main() -> None:
    """〔この関数がすること〕 非同期ランナーを起動します。"""

    try:
        exit_code = asyncio.run(_run())
    except KeyboardInterrupt:
        exit_code = 130
    except Exception as e:  # pragma: no cover
        logger.exception("recorder failed: %s", e)
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

