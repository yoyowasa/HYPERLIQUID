"""
役割:
- pfpl の "timestamp,level,(pid),message" ログから、日末(EOD)の paper 状態を抜き出す
- 対象は dry_run の paper でも live でも同じ（ログの message から state を拾うだけ）

抽出キー設計（どの行を state 更新として扱うか）:
- message に以下のキーワードが含まれる行だけを「state候補」として処理する
  * PAPER_PNL
  * pos_limit
  * paper_pos

抜きたい値（あれば拾う）:
- paper_pos / pos
- mid
- paper_unrealized / unrealized
- paper_realized / realized_cum / realized_total / cum（累積 realized が入っていれば）
- paper_avg_px / avg_px / entry_px（unrealized が無い場合の推定に使える）
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

_STATE_LINE_KEYWORDS = (
    "PAPER_PNL",
    "pos_limit",
    "paper_pos",
)

_NUM_KEYS = {
    "pos": ("paper_pos", "pos"),
    "mid": ("mid",),
    "unrealized": ("paper_unrealized", "unrealized"),
    "realized_cum": ("paper_realized", "realized_cum", "realized_total", "cum"),
    "avg_px": ("paper_avg_px", "avg_px", "entry_px", "avg_entry_px"),
}

_SIDE_KEYS = ("side",)


@dataclass
class PaperEOD:
    eod_ts: Optional[str] = None
    paper_pos: Optional[float] = None
    mid: Optional[float] = None
    unrealized: Optional[float] = None
    realized_cum: Optional[float] = None
    avg_px: Optional[float] = None

    def pos_usd(self) -> Optional[float]:
        if self.paper_pos is None or self.mid is None:
            return None
        return abs(self.paper_pos) * self.mid

    def total_pnl_cum(self) -> Optional[float]:
        if self.realized_cum is None or self.unrealized is None:
            return None
        return self.realized_cum + self.unrealized


def _split_log_line(line: str) -> Optional[tuple[str, str, str, str]]:
    # 役割: "timestamp,level,(pid),message" を「先頭2〜3カンマだけ」で分割して message を壊さない
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

    # pid がある場合のみ 3 つ目のカンマで切る（pid は数字のみを想定）
    i3 = rest.find(",")
    if i3 != -1:
        head = rest[:i3].strip()
        if head.isdigit():
            pid = head
            msg = rest[i3 + 1 :].strip()
            return ts, level, pid, msg

    # pid 無し（ts,level,message）
    return ts, level, "", rest


def _looks_like_state_line(msg: str) -> bool:
    # 役割: state 更新対象になる行だけ通す（高速化＋誤爆防止）
    low = msg.lower()
    return any(k.lower() in low for k in _STATE_LINE_KEYWORDS)


def _try_parse_dict_from_message(msg: str) -> Optional[dict[str, Any]]:
    # 役割: message 内の "{...}" を Python dict として取り出して ast.literal_eval する
    left = msg.find("{")
    right = msg.rfind("}")
    if left == -1 or right == -1 or right <= left:
        return None

    blob = msg[left : right + 1]
    try:
        obj = ast.literal_eval(blob)
    except Exception:
        return None

    if isinstance(obj, dict):
        return obj
    return None


def _extract_float_from_text(msg: str, key: str) -> Optional[float]:
    # 役割: "key=1.23" / "key: -4.56" / "'key': +7.89" を雑に拾う
    pat = re.compile(
        rf"(?:['\"])?{re.escape(key)}(?:['\"])?\s*[:=]\s*([+-]?\d+(?:\.\d+)?)"
    )
    m = pat.search(msg)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _pick_first_float(obj: dict[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    # 役割: dict から候補キーを順に見て、float 化できる最初の値を返す
    for k in keys:
        if k not in obj:
            continue
        v = obj.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except Exception:
            if isinstance(v, str):
                m = re.search(r"[+-]?\d+(?:\.\d+)?", v)
                if m:
                    try:
                        return float(m.group(0))
                    except Exception:
                        pass
            continue
    return None


def _extract_side(obj: dict[str, Any], msg: str) -> Optional[str]:
    # 役割: side を "BUY"/"SELL" で拾えるなら拾う（将来の拡張用）
    for k in _SIDE_KEYS:
        if k in obj:
            v = obj.get(k)
            if isinstance(v, str):
                vv = v.upper()
                if vv in ("BUY", "SELL"):
                    return vv
    m = re.search(r"\bside\b\s*[:=]\s*['\"]?(BUY|SELL)\b", msg, flags=re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def extract_paper_eod(path: str | Path) -> PaperEOD:
    """
    役割:
    - 1ファイルを上から走査し、最後に観測できた paper state を日末(EOD)として返す
    """
    p = Path(path)
    eod = PaperEOD()

    with p.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            row = _split_log_line(line)
            if row is None:
                continue

            ts, _level, _pid, msg = row
            if not _looks_like_state_line(msg):
                continue

            payload = _try_parse_dict_from_message(msg) or {}
            for nested_key in ("payload", "extra", "state"):
                nested = payload.get(nested_key)
                if isinstance(nested, dict):
                    payload = {**payload, **nested}

            pos = _pick_first_float(payload, _NUM_KEYS["pos"])
            mid = _pick_first_float(payload, _NUM_KEYS["mid"])
            unreal = _pick_first_float(payload, _NUM_KEYS["unrealized"])
            realized_cum = _pick_first_float(payload, _NUM_KEYS["realized_cum"])
            avg_px = _pick_first_float(payload, _NUM_KEYS["avg_px"])

            # dict で拾えなかったキーはテキストから拾う
            if pos is None:
                for k in _NUM_KEYS["pos"]:
                    pos = _extract_float_from_text(msg, k)
                    if pos is not None:
                        break
            if mid is None:
                for k in _NUM_KEYS["mid"]:
                    mid = _extract_float_from_text(msg, k)
                    if mid is not None:
                        break
            if unreal is None:
                for k in _NUM_KEYS["unrealized"]:
                    unreal = _extract_float_from_text(msg, k)
                    if unreal is not None:
                        break
            if realized_cum is None:
                for k in _NUM_KEYS["realized_cum"]:
                    realized_cum = _extract_float_from_text(msg, k)
                    if realized_cum is not None:
                        break
            if avg_px is None:
                for k in _NUM_KEYS["avg_px"]:
                    avg_px = _extract_float_from_text(msg, k)
                    if avg_px is not None:
                        break

            # state を更新（値が取れたものだけ上書き）
            updated = False
            if pos is not None:
                eod.paper_pos = pos
                updated = True
            if mid is not None:
                eod.mid = mid
                updated = True
            if unreal is not None:
                eod.unrealized = unreal
                updated = True
            if realized_cum is not None:
                eod.realized_cum = realized_cum
                updated = True
            if avg_px is not None:
                eod.avg_px = avg_px
                updated = True

            # unrealized が無い場合、mid と avg_px と pos が揃えば推定する
            if (
                eod.unrealized is None
                and eod.mid is not None
                and eod.avg_px is not None
                and eod.paper_pos is not None
            ):
                eod.unrealized = (eod.mid - eod.avg_px) * eod.paper_pos
                updated = True

            if updated:
                eod.eod_ts = ts

    return eod


def _to_csv_row(path: Path, eod: PaperEOD) -> str:
    # 役割: CSV 1 行に整形して返す（print するだけ）
    d = asdict(eod)
    pos_usd = eod.pos_usd()
    total = eod.total_pnl_cum()
    cols = [
        str(path),
        d.get("eod_ts") or "",
        "" if d.get("paper_pos") is None else f"{d['paper_pos']:.10f}",
        "" if d.get("mid") is None else f"{d['mid']:.6f}",
        "" if pos_usd is None else f"{pos_usd:.6f}",
        "" if d.get("unrealized") is None else f"{d['unrealized']:.6f}",
        "" if d.get("realized_cum") is None else f"{d['realized_cum']:.6f}",
        "" if total is None else f"{total:.6f}",
    ]
    return ",".join(cols)


def main(argv: list[str]) -> int:
    # 役割: CLI（複数ファイルを渡すと、それぞれの EOD を CSV で出す）
    if len(argv) <= 1:
        print(
            "usage: python src/hl_core/utils/pfpl_paper_eod.py <logfile1> [<logfile2> ...]"
        )
        return 2

    paths = [Path(a) for a in argv[1:]]
    print("file,eod_ts,paper_pos,mid,pos_usd,unrealized,realized_cum,total_pnl_cum")
    for p in paths:
        eod = extract_paper_eod(p)
        print(_to_csv_row(p, eod))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
