"""WebSocket recorder for Hyperliquid feeds.

This script subscribes to the level2, blocks, and trades streams and persists
the raw events to disk in either JSONL or Parquet format. Files rotate at a
configurable interval so long-running captures stay manageable. Parquet output
requires ``pyarrow``; if it is unavailable the recorder automatically falls
back to JSONL.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import importlib
import json
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from hl_core.utils.logger import get_logger

logger = get_logger("VRLG.recorder")


# ────────────────────────────── 出力先（JSONL / Parquet） ──────────────────────────────


@dataclass
class _SinkState:
    """Tracking object for a single stream's current output file."""

    path: Path
    opened_at: float
    fp: Optional[Any] = None
    buf: Optional[list] = None


def _load_pyarrow() -> tuple[Any, Any]:
    """Attempt to import pyarrow modules lazily."""

    pa = importlib.import_module("pyarrow")
    pq = importlib.import_module("pyarrow.parquet")
    return pa, pq


class RotatingSink:
    """Rotate files on a fixed cadence while writing JSONL or Parquet events."""

    def __init__(self, out_dir: Path, fmt: str, roll_secs: int) -> None:
        self.out_dir = out_dir
        self.fmt = fmt.lower()
        self.roll_secs = int(roll_secs)
        self.states: Dict[str, _SinkState] = {}
        self._parquet_ok = False
        if self.fmt == "parquet":
            try:
                _load_pyarrow()
                self._parquet_ok = True
            except ModuleNotFoundError:
                logger.warning("pyarrow not found; falling back to JSONL")
                self.fmt = "jsonl"
            except Exception:
                logger.warning("pyarrow failed to initialise; falling back to JSONL")
                self.fmt = "jsonl"

        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _new_path(self, stream: str, now: float) -> Path:
        timestamp = dt.datetime.utcfromtimestamp(now).strftime("%Y%m%d-%H%M%S")
        ext = "parquet" if self.fmt == "parquet" else "jsonl"
        return self.out_dir / f"{stream}-{timestamp}.{ext}"

    def _ensure_open(self, stream: str, now: float) -> _SinkState:
        st = self.states.get(stream)
        need_new = (st is None) or ((now - st.opened_at) >= self.roll_secs)
        if not need_new:
            assert st is not None
            return st

        if st:
            self._close_state(stream, st)

        path = self._new_path(stream, now)
        if self.fmt == "jsonl":
            fp = path.open("a", encoding="utf-8")
            st = _SinkState(path=path, opened_at=now, fp=fp)
        else:
            st = _SinkState(path=path, opened_at=now, buf=[])
        self.states[stream] = st
        logger.info("opened %s", path)
        return st

    def _close_state(self, stream: str, st: _SinkState) -> None:
        if self.fmt == "jsonl":
            with contextlib.suppress(Exception):
                if st.fp:
                    st.fp.flush()
                    st.fp.close()
        else:
            buf = st.buf or []
            if buf:
                try:
                    pa, pq = _load_pyarrow()
                    keys = sorted({k for rec in buf for k in rec.keys()})
                    arrays = {k: [rec.get(k) for rec in buf] for k in keys}
                    table = pa.table(arrays)
                    pq.write_table(table, st.path)
                except ModuleNotFoundError as exc:
                    logger.error(
                        "write parquet failed (%s); falling back to JSONL", exc
                    )
                    self._write_jsonl_fallback(st.path, buf)
                except Exception as e:
                    logger.error(
                        "write parquet failed (%s); falling back to JSONL", e
                    )
                    self._write_jsonl_fallback(st.path, buf)
        logger.info("closed %s", st.path)

    @staticmethod
    def _write_jsonl_fallback(path: Path, buf: list[Dict[str, Any]]) -> None:
        jpath = path.with_suffix(".jsonl")
        with jpath.open("a", encoding="utf-8") as fp:
            for rec in buf:
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def write(self, stream: str, rec: Dict[str, Any]) -> None:
        now = float(rec.get("t", time.time()))
        st = self._ensure_open(stream, now)
        if self.fmt == "jsonl":
            try:
                st.fp.write(json.dumps(rec, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
            except Exception as e:
                logger.debug("jsonl write failed: %s", e)
        else:
            st.buf.append(rec)  # type: ignore[union-attr]

    def close(self) -> None:
        for stream, st in list(self.states.items()):
            with contextlib.suppress(Exception):
                self._close_state(stream, st)


# ────────────────────────────── WS購読（level2 / blocks / trades） ──────────────────────────────


async def _consume_level2(
    symbol: str, sink: RotatingSink, stop: asyncio.Event
) -> None:
    try:
        from hl_core.api.ws import subscribe_level2  # type: ignore
    except Exception:
        logger.error("level2 WS adapter not available; skip")
        return

    async for book in subscribe_level2(symbol):
        if stop.is_set():
            break
        try:
            rec = {
                "t": float(
                    getattr(book, "t", None)
                    or getattr(book, "timestamp", None)
                    or time.time()
                ),
                "best_bid": float(getattr(book, "best_bid", 0.0)),
                "best_ask": float(getattr(book, "best_ask", 0.0)),
                "bid_size_l1": float(getattr(book, "bid_size_l1", 0.0)),
                "ask_size_l1": float(getattr(book, "ask_size_l1", 0.0)),
            }
            sink.write("level2", rec)
        except Exception:
            continue


async def _consume_blocks(sink: RotatingSink, stop: asyncio.Event) -> None:
    try:
        from hl_core.api.ws import subscribe_blocks  # type: ignore
    except Exception:
        logger.warning("blocks WS adapter not available; skip")
        return

    async for blk in subscribe_blocks():
        if stop.is_set():
            break
        rec = {
            "t": float(getattr(blk, "timestamp", None) or time.time()),
            "height": int(getattr(blk, "height", -1)),
        }
        sink.write("blocks", rec)


async def _consume_trades(
    symbol: str, sink: RotatingSink, stop: asyncio.Event
) -> None:
    try:
        from hl_core.api.ws import subscribe_trades  # type: ignore
    except Exception:
        logger.warning("trades WS adapter not available; skip")
        return

    async for tr in subscribe_trades(symbol):
        if stop.is_set():
            break
        side = str(getattr(tr, "side", "")).upper()
        price = float(getattr(tr, "price", 0.0))
        size = float(getattr(tr, "size", 0.0))
        t = float(
            getattr(tr, "t", None) or getattr(tr, "timestamp", None) or time.time()
        )
        sink.write("trades", {"t": t, "side": side, "price": price, "size": size})


# ────────────────────────────── CLI & ランナー ──────────────────────────────


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Record Hyperliquid WS streams to local files."
    )
    p.add_argument("--symbol", default="BTCUSD-PERP", help="symbol to record")
    p.add_argument("--out-dir", default="data/recordings", help="output directory")
    p.add_argument(
        "--format",
        choices=["jsonl", "parquet"],
        default="parquet",
        help="output format",
    )
    p.add_argument(
        "--roll-secs",
        type=int,
        default=600,
        help="file rotation interval in seconds",
    )
    p.add_argument("--no-trades", action="store_true", help="skip trades stream")
    p.add_argument("--no-blocks", action="store_true", help="skip blocks stream")
    p.add_argument("--log-level", default="INFO", help="logger level")
    return p.parse_args(list(argv) if argv is not None else None)


async def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    try:
        logger.setLevel(str(args.log_level).upper())
    except Exception:
        pass

    sink = RotatingSink(Path(args.out_dir), args.format, args.roll_secs)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _set_stop(*_: object) -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _set_stop)

    tasks = [
        asyncio.create_task(
            _consume_level2(args.symbol, sink, stop), name="ws_level2"
        )
    ]
    if not args.no_blocks:
        tasks.append(
            asyncio.create_task(_consume_blocks(sink, stop), name="ws_blocks")
        )
    if not args.no_trades:
        tasks.append(
            asyncio.create_task(_consume_trades(args.symbol, sink, stop), name="ws_trades")
        )

    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        sink.close()

    return 0


def run() -> None:
    try:
        import uvloop  # type: ignore

        uvloop.install()
    except Exception:
        pass

    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:  # pragma: no cover - logging only
        logger.exception("recorder failed: %s", exc)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    run()

