"""
役割:
- pfpl の "timestamp,level,(pid),message" ログ（1日=1ファイル想定）を集計して CSV で出力する
- decision / spread_false / ALL_OK+signal / orders(paper) / BUY/SELL / realized_sum / WARNING/ERROR を日別に出す
- さらに Step2 の extract_paper_eod() を呼び、日末(EOD)の paper_pos / mid / unrealized / realized_cum も同じ行に結合する
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# 役割: 「スクリプト直実行」と「-m 実行」の両方で import が動くようにする
try:
    from hl_core.utils.pfpl_paper_eod import extract_paper_eod
except Exception:
    sys.path.append(str(Path(__file__).resolve().parents[2]))  # .../src を追加
    from hl_core.utils.pfpl_paper_eod import extract_paper_eod

_FLOAT = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"

_DECISION_RE = re.compile(r"\bdecision\b", re.IGNORECASE)
_SPREAD_FALSE_RE = re.compile(
    r"spread\(px\)\s*<=\s*"
    + _FLOAT
    + r"\s*:\s*False\b",
    re.IGNORECASE,
)

_ORDER_STATUS_RE = re.compile(r"\bORDER_STATUS\b", re.IGNORECASE)
_PAPER_PNL_RE = re.compile(r"\bPAPER_PNL\b", re.IGNORECASE)

_SIDE_RE = re.compile(r"\bside\b\s*[:=]\s*['\"]?(BUY|SELL)\b", re.IGNORECASE)
_STATUS_PAPER_RE = re.compile(r"\bstatus\b\s*[:=]\s*['\"]?paper\b", re.IGNORECASE)
_DRYRUN_RE = re.compile(r"\[DRY-RUN\]", re.IGNORECASE)
_DRYRUN_SIDE_RE = re.compile(r"\[DRY-RUN\]\s+(BUY|SELL)\b", re.IGNORECASE)

_BOOT_RE = re.compile(r"\bboot\b\s*[:=]", re.IGNORECASE)
# 役割: skip理由（min_usd / pos_limit dust / pos_limit skip / decision段階dust）をログから数えるための正規表現
_MIN_USD_SKIP_RE = re.compile(r"<\s*min_usd\b.*?\bskip\b", re.IGNORECASE)
# pos_limit dust により order を出さない（新ログ形式）行を検出する
_POS_LIMIT_DUST_SKIP_RE = re.compile(r"\bpos_ok=False\s*\(pos_limit dust\)\s*stage\s*=\s*order\b", re.IGNORECASE)
# 役割: decision段階の dust ブロック（"pos_ok=False (pos_limit dust): ..."）を確実にカウントする
_POS_OK_DUST_FALSE_RE = re.compile(r"pos_ok=False\s*\(pos_limit dust\)", re.IGNORECASE)
_POS_LIMIT_SKIP_RE = re.compile(r"\bpos_limit skip\b|\bposition limit\b.*\breached\b", re.IGNORECASE)


@dataclass
class DailyRow:
    # 役割: 1ファイル分（日別）の集計結果を保持する
    file: str
    decision: int = 0
    spread_false: int = 0
    all_ok_signal: int = 0

    orders: int = 0
    paper_buy: int = 0
    paper_sell: int = 0

    paper_trades: int = 0
    realized_sum: float = 0.0

    warn: int = 0
    err: int = 0
    boot: int = 0
    unique_pid_cnt: int = 0
    # 役割: スキップ理由の内訳（ログからカウント）
    min_usd_skip: int = 0
    pos_limit_dust_skip: int = 0
    pos_ok_dust_false: int = 0
    pos_limit_skip: int = 0

    diff_mean_mid_fair: Optional[float] = None

    eod_ts: Optional[str] = None
    eod_paper_pos: Optional[float] = None
    eod_mid: Optional[float] = None
    eod_pos_usd: Optional[float] = None
    eod_unrealized: Optional[float] = None
    eod_realized_cum: Optional[float] = None
    eod_total_pnl_cum: Optional[float] = None


def _split_log_line(line: str) -> Optional[tuple[str, str, str, str]]:
    # 役割:
    # - "timestamp,level,(pid),message" を「先頭2カンマ + (必要ならpid分)」だけで分割し、
    #   message 内のカンマ（CSV クォート）で壊れないようにする
    # - pid が無いログ（timestamp,level,message）も許容する
    s = line.rstrip("\n")

    i1 = s.find(",")
    if i1 == -1:
        return None
    i2 = s.find(",", i1 + 1)
    if i2 == -1:
        return None

    ts = s[:i1].strip()
    level = s[i1 + 1 : i2].strip()
    rest = s[i2 + 1 :].strip()

    # pid がある場合だけ 1 回だけ追加で分割する（pid は数字のみ想定）
    i3 = rest.find(",")
    if i3 != -1:
        head = rest[:i3].strip()
        if head.isdigit():
            pid = head
            msg = rest[i3 + 1 :].strip()
            return ts, level, pid, msg

    # pid 無し（ts,level,message）
    return ts, level, "", rest


def _try_parse_dict_from_message(msg: str) -> Optional[dict[str, Any]]:
    # 役割: message 内に "{...}" がある場合、Python dict として読み取る（無ければ None）
    left = msg.find("{")
    right = msg.rfind("}")
    if left == -1 or right == -1 or right <= left:
        return None
    blob = msg[left : right + 1]
    try:
        obj = ast.literal_eval(blob)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _extract_float_from_text(msg: str, key: str) -> Optional[float]:
    # 役割: "key=1.23" / "key: -4.56" / "'key': 7.89" を拾う
    pat = re.compile(rf"(?:['\"])?{re.escape(key)}(?:['\"])?\s*[:=]\s*({_FLOAT})")
    m = pat.search(msg)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _pick_first_float(payload: dict[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    # 役割: payload(dict) から候補キーを順に見て、float 化できる最初の値を返す
    for k in keys:
        if k not in payload:
            continue
        v = payload.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except Exception:
            continue
    return None


def _extract_first(msg: str, payload: dict[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    # 役割: dict から拾えなければ、テキストから拾う（2段構え）
    v = _pick_first_float(payload, keys)
    if v is not None:
        return v
    for k in keys:
        vv = _extract_float_from_text(msg, k)
        if vv is not None:
            return vv
    return None


def _is_paper_order(msg: str) -> bool:
    # 役割: ORDER_STATUS のうち、paper(dry_run) の注文だけを数える
    if not _ORDER_STATUS_RE.search(msg):
        return False
    if _DRYRUN_RE.search(msg):
        return True
    if _STATUS_PAPER_RE.search(msg):
        return True
    return False


def _is_all_ok_signal_from_decision_line(msg: str) -> bool:
    # 役割:
    # - ログに "ALL_OK+signal" マーカーが無い場合でも、
    #   decision 行の「ガード全通過 + (long|short)」を ALL_OK+signal として数える
    if "abs>=" not in msg or "pct(mode" not in msg or "spread(px)<=" not in msg:
        return False

    # abs/pct/spread が False なら不合格
    if re.search(r"abs>=\s*" + _FLOAT + r"\s*:\s*False\b", msg, flags=re.IGNORECASE):
        return False
    if re.search(
        r"pct\(mode=[^)]+\)>=\s*" + _FLOAT + r"\s*:\s*False\b",
        msg,
        flags=re.IGNORECASE,
    ):
        return False
    if _SPREAD_FALSE_RE.search(msg):
        return False

    # その他ガードが False なら不合格
    if re.search(r"cooldown_ok=False\b", msg, flags=re.IGNORECASE):
        return False
    if re.search(r"pos_ok=False\b", msg, flags=re.IGNORECASE):
        return False
    if re.search(r"notional_ok=False\b", msg, flags=re.IGNORECASE):
        return False
    if re.search(r"funding_ok=False\b", msg, flags=re.IGNORECASE):
        return False

    # シグナルが無ければ不合格
    if not re.search(r"\blong=True\b|\bshort=True\b", msg, flags=re.IGNORECASE):
        return False

    return True


def summarize_file(path: Path) -> DailyRow:
    # 役割: 1ログファイルを走査して日別指標+EOD を作る
    decision = 0
    spread_false = 0
    all_ok_signal = 0

    orders = 0
    # 役割: BUY/SELL は (1) dry-run 行優先、無ければ (2) ORDER_STATUS の side を拾う
    dry_buy = 0
    dry_sell = 0
    order_buy = 0
    order_sell = 0

    paper_trades = 0
    realized_sum = 0.0

    warn = 0
    err = 0
    boot = 0
    pids: set[str] = set()
    # 役割: スキップ理由カウンタ（日別）
    min_usd_skip = 0
    pos_limit_dust_skip = 0
    pos_ok_dust_false = 0
    pos_limit_skip = 0

    diff_sum = 0.0
    diff_cnt = 0

    prev_realized_cum: Optional[float] = None

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            row = _split_log_line(line)
            if row is None:
                continue

            _ts, level, pid, msg = row

            if level.upper() == "WARNING":
                warn += 1
            elif level.upper() == "ERROR":
                err += 1

            pid_norm = pid.strip().strip("(").strip(")")
            if pid_norm:
                pids.add(pid_norm)

            if _BOOT_RE.search(msg):
                boot += 1

            payload = _try_parse_dict_from_message(msg) or {}
            # 役割: 1行ログからスキップ理由を拾って日別カウンタを増やす
            if _MIN_USD_SKIP_RE.search(msg):
                min_usd_skip += 1
            if _POS_LIMIT_DUST_SKIP_RE.search(msg):
                pos_limit_dust_skip += 1
            if _POS_OK_DUST_FALSE_RE.search(msg):
                pos_ok_dust_false += 1
            if _POS_LIMIT_SKIP_RE.search(msg):
                pos_limit_skip += 1

            # dry-run のサイド（BUY/SELL）を別でカウント（ORDER_STATUS に side が無いケースが多い）
            m_dry = _DRYRUN_SIDE_RE.search(msg)
            if m_dry:
                side = m_dry.group(1).upper()
                if side == "BUY":
                    dry_buy += 1
                elif side == "SELL":
                    dry_sell += 1

            if _DECISION_RE.search(msg):
                decision += 1

                if _SPREAD_FALSE_RE.search(msg):
                    spread_false += 1

                if _is_all_ok_signal_from_decision_line(msg):
                    all_ok_signal += 1

                mid = _extract_first(msg, payload, ("mid", "mid_px", "mid_price"))
                fair = _extract_first(
                    msg, payload, ("fair", "fair_px", "fair_price", "fairPrice")
                )
                if mid is not None and fair is not None:
                    diff_sum += mid - fair
                    diff_cnt += 1

            if _is_paper_order(msg):
                orders += 1
                m = _SIDE_RE.search(msg)
                if m:
                    side = m.group(1).upper()
                    if side == "BUY":
                        order_buy += 1
                    elif side == "SELL":
                        order_sell += 1

            if _PAPER_PNL_RE.search(msg):
                # 役割: realized の「増分」を優先して拾う。無ければ累積(paper_realized等)の差分で増分を作る。
                realized_delta: Optional[float] = None
                m = re.search(
                    rf"(?<!paper_)\brealized\b\s*[:=]\s*({_FLOAT})",
                    msg,
                    flags=re.IGNORECASE,
                )
                if m:
                    try:
                        realized_delta = float(m.group(1))
                    except Exception:
                        realized_delta = None

                if realized_delta is None:
                    realized_delta = _extract_first(
                        msg,
                        payload,
                        ("realized_delta", "delta_realized", "d_realized"),
                    )

                realized_cum = _extract_first(
                    msg,
                    payload,
                    # pfpl の PAPER_PNL は "cum=" を使うことがあるので含める
                    ("paper_realized", "realized_cum", "realized_total", "cum"),
                )

                if realized_delta is not None:
                    realized_sum += realized_delta
                    paper_trades += 1
                elif realized_cum is not None and prev_realized_cum is not None:
                    realized_sum += realized_cum - prev_realized_cum
                    paper_trades += 1

                if realized_cum is not None:
                    prev_realized_cum = realized_cum

    diff_mean = (diff_sum / diff_cnt) if diff_cnt > 0 else None

    eod = extract_paper_eod(path)

    # BUY/SELL は dry-run が取れていればそれを優先（無ければ ORDER_STATUS 由来を使う）
    paper_buy = dry_buy if (dry_buy + dry_sell) > 0 else order_buy
    paper_sell = dry_sell if (dry_buy + dry_sell) > 0 else order_sell

    row_out = DailyRow(
        file=str(path),
        decision=decision,
        spread_false=spread_false,
        all_ok_signal=all_ok_signal,
        orders=orders,
        paper_buy=paper_buy,
        paper_sell=paper_sell,
        paper_trades=paper_trades,
        realized_sum=realized_sum,
        warn=warn,
        err=err,
        boot=boot,
        unique_pid_cnt=len(pids),
        # 役割: スキップ理由（ログから集計した値）
        min_usd_skip=min_usd_skip,
        pos_limit_dust_skip=pos_limit_dust_skip,
        pos_ok_dust_false=pos_ok_dust_false,
        pos_limit_skip=pos_limit_skip,
        diff_mean_mid_fair=diff_mean,
        eod_ts=eod.eod_ts,
        eod_paper_pos=eod.paper_pos,
        eod_mid=eod.mid,
        eod_pos_usd=eod.pos_usd(),
        eod_unrealized=eod.unrealized,
        eod_realized_cum=eod.realized_cum,
        eod_total_pnl_cum=eod.total_pnl_cum(),
    )
    return row_out


def _fmt_float(x: Optional[float], nd: int = 6) -> str:
    # 役割: None を空欄、数値は固定小数で出す
    if x is None:
        return ""
    return f"{x:.{nd}f}"


def _row_to_csv(row: DailyRow) -> str:
    # 役割: DailyRow を CSV 1行にする（あなたの表 + EOD 列）
    cols = [
        row.file,
        _fmt_float(row.diff_mean_mid_fair, 4),
        str(row.decision),
        str(row.spread_false),
        str(row.all_ok_signal),
        str(row.orders),
        str(row.paper_buy),
        str(row.paper_sell),
        _fmt_float(row.realized_sum, 6),
        str(row.warn),
        str(row.err),
        str(row.boot),
        str(row.unique_pid_cnt),
        str(row.min_usd_skip),
        str(row.pos_limit_dust_skip),
        str(row.pos_ok_dust_false),
        str(row.pos_limit_skip),
        row.eod_ts or "",
        _fmt_float(row.eod_paper_pos, 10),
        _fmt_float(row.eod_mid, 6),
        _fmt_float(row.eod_pos_usd, 6),
        _fmt_float(row.eod_unrealized, 6),
        _fmt_float(row.eod_realized_cum, 6),
        _fmt_float(row.eod_total_pnl_cum, 6),
    ]
    return ",".join(cols)


def _expand_paths(args: list[str]) -> list[Path]:
    # 役割: ワイルドカード/ディレクトリ/ファイルをまとめて Path のリストにする
    out: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            out.extend(sorted(p.glob("*.csv*")))
            continue
        if any(ch in a for ch in ("*", "?", "[")):
            out.extend(sorted(Path().glob(a)))
            continue
        out.append(p)
    return out


def main(argv: list[str]) -> int:
    # 役割: CLI。複数ファイルを渡すと日別+TOTAL を CSV で出す
    if len(argv) <= 1:
        print("usage: python -m hl_core.utils.pfpl_daily_summary <logfile... or glob... or dir>")
        return 2

    paths = _expand_paths(argv[1:])
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("no files")
        return 1

    # 役割: 日別サマリCSVのヘッダ（スキップ理由列を追加）
    print("file,diff_mean(mid-fair),decision,spread_false,ALL_OK+signal,orders,paper_buy,paper_sell,realized_sum,WARNING,ERROR,boot_cnt,unique_pid_cnt,min_usd_skip,pos_limit_dust_skip,pos_ok_dust_false,pos_limit_skip,eod_ts,eod_paper_pos,eod_mid,eod_pos_usd,eod_unrealized,eod_realized_cum,eod_total_pnl_cum")

    total = DailyRow(file="TOTAL")
    diff_sum = 0.0
    diff_cnt = 0.0

    for p in paths:
        r = summarize_file(p)
        print(_row_to_csv(r))

        total.decision += r.decision
        total.spread_false += r.spread_false
        total.all_ok_signal += r.all_ok_signal
        total.orders += r.orders
        total.paper_buy += r.paper_buy
        total.paper_sell += r.paper_sell
        total.paper_trades += r.paper_trades
        total.realized_sum += r.realized_sum
        total.warn += r.warn
        total.err += r.err
        total.boot += r.boot
        # 役割: TOTAL にスキップ理由カウンタを足し込む
        total.min_usd_skip += r.min_usd_skip
        total.pos_limit_dust_skip += r.pos_limit_dust_skip
        total.pos_ok_dust_false += r.pos_ok_dust_false
        total.pos_limit_skip += r.pos_limit_skip

        if r.diff_mean_mid_fair is not None and r.decision > 0:
            # 役割:
            # - 日別平均の単純平均ではなく、観測回数で重みづけしたい
            # - summarize_file() 内の diff_cnt を持っていないため、近似として decision を重みとして使う
            diff_sum += r.diff_mean_mid_fair * r.decision
            diff_cnt += r.decision

    total.diff_mean_mid_fair = (diff_sum / diff_cnt) if diff_cnt > 0 else None
    print(_row_to_csv(total))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
