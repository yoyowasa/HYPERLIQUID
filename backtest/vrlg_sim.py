# 〔このスクリプトがすること〕
# 録画した L2（level2-*.jsonl/parquet）から 100ms 足で特徴量を生成し、
# RotationDetector と SignalDetector を通して「post-only 指値→TTL or スプレッド縮小で解消」
# の最小モデルでバックテストします。I/Oはローカルファイルのみで完結します。

from __future__ import annotations

import argparse
import glob
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

# 〔この import がすること〕 共通構成/部品を VRLG から再利用します
try:
    from bots.vrlg.config import coerce_vrlg_config  # type: ignore
    from bots.vrlg.rotation_detector import RotationDetector  # type: ignore
    from bots.vrlg.signal_detector import SignalDetector  # type: ignore
    from bots.vrlg.data_feed import FeatureSnapshot  # type: ignore
    from bots.vrlg.size_allocator import SizeAllocator  # type: ignore
    from bots.vrlg.risk_management import RiskManager  # type: ignore
except Exception:
    from src.bots.vrlg.config import coerce_vrlg_config  # type: ignore
    from src.bots.vrlg.rotation_detector import RotationDetector  # type: ignore
    from src.bots.vrlg.signal_detector import SignalDetector  # type: ignore
    from src.bots.vrlg.data_feed import FeatureSnapshot  # type: ignore
    from src.bots.vrlg.size_allocator import SizeAllocator  # type: ignore
    from src.bots.vrlg.risk_management import RiskManager  # type: ignore


# ─────────────────────────────── データ構造（テスト結果など） ───────────────────────────────


@dataclass
class Trade:
    """〔このデータクラスがすること〕 1 トレード（片側）を記録します。"""

    side: str
    t_entry: float
    px_entry: float
    ref_mid_at_fill: float
    t_exit: float
    px_exit: float

    def holding_ms(self) -> float:
        """〔このメソッドがすること〕 保有時間（ms）を返します。"""

        return max(0.0, (self.t_exit - self.t_entry) * 1000.0)

    def pnl_bps(self) -> float:
        """〔このメソッドがすること〕 損益（bps, 手数料考慮なしの単純差）を返します。"""

        # bps = (exit/entry - 1) * 1e4 （BUY） / 逆符号（SELL）
        if self.px_entry <= 0:
            return 0.0
        ret = (self.px_exit / self.px_entry - 1.0) * 1e4
        return ret if self.side == "BUY" else -ret

    def slip_ticks(self, tick: float) -> float:
        """〔このメソッドがすること〕 充足滑り（ticks）を返します。"""

        if tick <= 0:
            return 0.0
        return abs(self.px_entry - self.ref_mid_at_fill) / tick


# ─────────────────────────────── 入力（録画ファイルの読取） ───────────────────────────────


def _iter_jsonl(files: List[Path]) -> Iterator[Dict[str, Any]]:
    """〔この関数がすること〕 level2-*.jsonl 群を時系列順にストリーム読み出しします。"""

    for fp in sorted(files):
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    yield rec
                except Exception:
                    continue


def _iter_parquet(files: List[Path]) -> Iterator[Dict[str, Any]]:
    """〔この関数がすること〕 level2-*.parquet 群を時系列順にストリーム読み出しします。"""

    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception:
        return iter(())
    for fp in sorted(files):
        try:
            table = pq.read_table(fp)  # type: ignore
        except Exception:
            continue
        cols = {name: table.column(name).to_pylist() for name in table.column_names}
        n = len(next(iter(cols.values()))) if cols else 0
        for i in range(n):
            yield {k: cols[k][i] for k in cols}


def load_level2_stream(data_dir: Path) -> Iterator[Tuple[float, float, float, float, float]]:
    """〔この関数がすること〕
    ディレクトリ内の level2-*.{jsonl,parquet} を見つけ、(t, best_bid, best_ask, bid_size_l1, ask_size_l1) を時系列で返します。
    """

    jsonl = [Path(p) for p in glob.glob(str(data_dir / "level2-*.jsonl"))]
    pq = [Path(p) for p in glob.glob(str(data_dir / "level2-*.parquet"))]
    it: Iterable[Dict[str, Any]]
    if pq:
        it = _iter_parquet(pq)
    elif jsonl:
        it = _iter_jsonl(jsonl)
    else:
        raise FileNotFoundError(f"No level2-*.jsonl/.parquet under {data_dir}")

    for rec in it:
        t = float(rec.get("t", time.time()))
        bb = float(rec.get("best_bid", 0.0))
        ba = float(rec.get("best_ask", 0.0))
        bs = float(rec.get("bid_size_l1", 0.0))
        asz = float(rec.get("ask_size_l1", 0.0))
        yield (t, bb, ba, bs, asz)


# ─────────────────────────────── 簡易 Fill モデルとシミュレータ ───────────────────────────────


@dataclass
class Order:
    """〔このデータクラスがすること〕 シミュレータ内の“掲示中の子注文”を表します。"""

    side: str
    price: float
    t_post: float
    t_expire: float
    display: float
    total: float
    filled: bool = False


class VRLGSimulator:
    """〔このクラスがすること〕
    録画 L2 を使って VRLG の約定/解消を最小モデルで再現し、指標を出力します。
    """

    def __init__(self, cfg: Any) -> None:
        """〔このメソッドがすること〕 設定を取り込み、主要コンポーネント（検出器/サイズ/Risk）を初期化します。"""

        self.cfg = coerce_vrlg_config(cfg)
        self.tick = float(self.cfg.symbol.tick_size)
        self.rot = RotationDetector(self.cfg)
        self.sig = SignalDetector(self.cfg)
        self.sizer = SizeAllocator(self.cfg)
        self.risk = RiskManager(self.cfg)

        # 実行パラメータ
        self.ttl_s = float(self.cfg.exec.order_ttl_ms) / 1000.0
        self.off_norm = float(self.cfg.exec.offset_ticks_normal)
        self.off_deep = float(self.cfg.exec.offset_ticks_deep)
        self.collapse_ticks = float(self.cfg.exec.spread_collapse_ticks)
        self.splits = max(1, int(self.cfg.exec.splits))
        self.side_mode = str(getattr(self.cfg.exec, "side_mode", "both")).lower()
        self.display_ratio = float(self.cfg.exec.display_ratio)
        self.min_display = float(self.cfg.exec.min_display_btc)

        # 結果蓄積
        self.trades: List[Trade] = []
        self.orders: List[Order] = []

        # 進行状況
        self._last_mid = None  # type: Optional[float]
        self._last_dob = 0.0
        self._last_spread = 0.0

    def _place_children(self, mid: float, deepen: bool, now: float, top_depth: float) -> None:
        """〔このメソッドがすること〕
        mid と deepen 指示から両面（あるいは片面）の子注文を作成し、掲示リストに追加します。
        """

        offset = self.off_deep if deepen else self.off_norm
        px_bid = round((mid - offset * self.tick) / self.tick) * self.tick
        px_ask = round((mid + offset * self.tick) / self.tick) * self.tick

        total = self.sizer.next_size(mid=mid, risk_mult=1.0)
        if total <= 0:
            return
        child_total = total / self.splits
        child_display = min(max(child_total * self.display_ratio, self.min_display), child_total)
        t_exp = now + self.ttl_s

        sides = [("BUY", px_bid), ("SELL", px_ask)]
        if self.side_mode == "buy":
            sides = [("BUY", px_bid)]
        elif self.side_mode == "sell":
            sides = [("SELL", px_ask)]

        # Risk: 板消費率に加算（display/TopDepth）
        if top_depth > 0:
            self.risk.register_order_post(display_size=child_display, top_depth=top_depth)

        for side, price in sides:
            for _ in range(self.splits):
                self.orders.append(
                    Order(
                        side=side,
                        price=price,
                        t_post=now,
                        t_expire=t_exp,
                        display=child_display,
                        total=child_total,
                    )
                )

    def _match_orders(self, bb: float, ba: float, mid: float, now: float) -> List[Tuple[Order, float]]:
        """〔このメソッドがすること〕
        掲示中の子注文を L1 に照らして“充足したもの”を抽出し、(order, ref_mid) を返します。
        簡易ルール: BUY は best_bid >= price、SELL は best_ask <= price の瞬間に約定。
        """

        filled: List[Tuple[Order, float]] = []
        for od in self.orders:
            if od.filled or now < od.t_post or now > od.t_expire:
                continue
            if od.side == "BUY" and bb >= od.price:
                od.filled = True
                filled.append((od, mid))
            elif od.side == "SELL" and ba <= od.price:
                od.filled = True
                filled.append((od, mid))
        return filled

    def _exit_price(self, bb: float, ba: float, mid: float) -> float:
        """〔このメソッドがすること〕 IOC 解消の約定価格を近似（mid 使用）で返します。"""

        return float(mid)

    def run(self, l2_stream: Iterator[Tuple[float, float, float, float, float]]) -> None:
        """〔このメソッドがすること〕
        L2 ストリームを 100ms 足相当で処理し、Signal→発注→約定→解消 の一連を再現します。
        """

        # ステップ状の 100ms サンプリングを作る
        dt = 0.1
        next_emit: Optional[float] = None

        # Exit 待ちの“ポジション”（充足済み子注文）
        open_fills: List[Tuple[str, float, float, float]] = []  # (side, t_fill, px_fill, ref_mid)

        for t, bb, ba, bs, asz in l2_stream:
            if next_emit is None:
                next_emit = t
            # 100ms ごとに 1 回だけ特徴量を出す（間の L1 は最新で上書き）
            if t < next_emit:
                self._last_mid = (bb + ba) / 2.0
                self._last_dob = bs + asz
                self._last_spread = (ba - bb) / max(self.tick, 1e-12)
                continue

            # 特徴量生成
            mid = (bb + ba) / 2.0
            spread_ticks = (ba - bb) / max(self.tick, 1e-12)
            dob = bs + asz
            obi = 0.0 if dob <= 0 else (bs - asz) / max(dob, 1e-9)
            snap = FeatureSnapshot(t=t, mid=mid, spread_ticks=spread_ticks, dob=dob, obi=obi)

            # 周期更新→位相付与
            self.rot.update(t, dob, spread_ticks)
            phase = self.rot.current_phase(t)
            snap = snap.with_phase(phase)

            # シグナル評価
            sig = self.sig.update_and_maybe_signal(t, snap)
            if sig and self.rot.is_active():
                adv = self.risk.advice()
                self._place_children(mid=sig.mid, deepen=adv.deepen_post_only, now=t, top_depth=dob)

            # 掲示中の注文の約定判定
            fills = self._match_orders(bb, ba, mid, now=t)
            for od, ref_mid in fills:
                # 滑りを記録（Risk 用）
                self.risk.register_fill(fill_price=od.price, ref_mid=ref_mid, tick_size=self.tick)
                # Exit 条件: スプレッドが collapse_ticks 以下 もしくは TTL 到達
                open_fills.append((od.side, t, od.price, ref_mid))

            # Exit 実行（spread 縮小 or TTL超過）
            still_open: List[Tuple[str, float, float, float]] = []
            for side, t_fill, px_fill, ref_mid in open_fills:
                elapsed = t - t_fill
                collapse = spread_ticks <= self.collapse_ticks
                timeout = elapsed >= self.ttl_s
                if collapse or timeout:
                    # IOC で解消
                    px_exit = self._exit_price(bb, ba, mid)
                    self.trades.append(
                        Trade(
                            side=side,
                            t_entry=t_fill,
                            px_entry=px_fill,
                            ref_mid_at_fill=ref_mid,
                            t_exit=t,
                            px_exit=px_exit,
                        )
                    )
                else:
                    still_open.append((side, t_fill, px_fill, ref_mid))
            open_fills = still_open

            # 100ms の刻みを進める
            next_emit += dt

        # ストリーム終端：開いているものは TTL 扱いでクローズ
        if open_fills:
            last_t = open_fills[-1][1]
            for side, t_fill, px_fill, ref_mid in open_fills:
                self.trades.append(
                    Trade(
                        side=side,
                        t_entry=t_fill,
                        px_entry=px_fill,
                        ref_mid_at_fill=ref_mid,
                        t_exit=last_t + self.ttl_s,
                        px_exit=px_fill,
                    )
                )

    # ───────────────── 結果の集計と表示 ─────────────────

    def summary(self) -> Dict[str, Any]:
        """〔このメソッドがすること〕 バックテスト指標を集計して辞書で返します。"""

        n = len(self.trades)
        if n == 0:
            return {"trades": 0}
        hit = sum(1 for tr in self.trades if tr.pnl_bps() > 0)
        pnl = [tr.pnl_bps() for tr in self.trades]
        hold = [tr.holding_ms() for tr in self.trades]
        slip = [tr.slip_ticks(self.tick) for tr in self.trades]
        pnl_cum = 0.0
        max_dd = 0.0
        peak = 0.0
        for x in pnl:
            pnl_cum += x
            peak = max(peak, pnl_cum)
            max_dd = max(max_dd, peak - pnl_cum)

        dur_s = max(1.0, (self.trades[-1].t_exit - self.trades[0].t_entry))
        trades_per_min = 60.0 * n / dur_s

        return {
            "trades": n,
            "hit_rate": hit / n,
            "avg_pnl_bps": statistics.mean(pnl),
            "median_pnl_bps": statistics.median(pnl),
            "avg_holding_ms": statistics.mean(hold),
            "avg_slip_ticks": statistics.mean(slip),
            "trades_per_min": trades_per_min,
            "max_drawdown_bps": max_dd,
        }

    def print_summary(self) -> None:
        """〔このメソッドがすること〕 集計結果を整形して標準出力へ表示します。"""

        s = self.summary()
        if s.get("trades", 0) == 0:
            print("No trades.")
            return
        print(
            "Trades={trades} | HitRate={hit:.1%} | AvgPnL={pnl:.2f}bps | Slip={slip:.2f}ticks | Hold={hold:.0f}ms | TPM={tpm:.2f} | MaxDD={dd:.1f}bps".format(
                trades=s["trades"],
                hit=s["hit_rate"],
                pnl=s["avg_pnl_bps"],
                slip=s["avg_slip_ticks"],
                hold=s["avg_holding_ms"],
                tpm=s["trades_per_min"],
                dd=s["max_drawdown_bps"],
            )
        )


# ─────────────────────────────── CLI エントリポイント ───────────────────────────────


def _load_config(path: str) -> Any:
    """〔この関数がすること〕 設定ファイル（TOML/YAML）を読み込み、dict を返します。"""

    # 可能なら共通ローダを使う
    try:
        from hl_core.utils.config import load_config  # type: ignore

        return load_config(path)
    except Exception:
        pass
    # TOML の軽量フォールバック
    try:
        import tomllib  # py3.11+

        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        import tomli  # type: ignore

        with open(path, "rb") as f:
            return tomli.load(f)


def parse_args() -> argparse.Namespace:
    """〔この関数がすること〕 CLI 引数を解釈します。"""

    p = argparse.ArgumentParser(description="VRLG backtest simulator (L2 replay, 100ms)")
    p.add_argument("--config", required=True, help="strategy config (TOML/YAML)")
    p.add_argument("--data-dir", required=True, help="directory containing level2-*.jsonl or *.parquet")
    p.add_argument("--max-rows", type=int, default=0, help="limit number of L2 rows for a quick run (0=all)")
    return p.parse_args()


def main() -> None:
    """〔この関数がすること〕 バックテストを実行し、主要指標を表示します。"""

    args = parse_args()
    raw_cfg = _load_config(args.config)
    sim = VRLGSimulator(raw_cfg)
    stream = load_level2_stream(Path(args.data_dir))

    # 行数制限（クイック試走用）
    if args.max_rows and args.max_rows > 0:

        def _limited(it, k):
            for i, rec in enumerate(it):
                if i >= k:
                    break
                yield rec

        stream = _limited(stream, args.max_rows)

    sim.run(stream)
    sim.print_summary()


if __name__ == "__main__":
    main()

