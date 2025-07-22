# diff.py を以下のように修正してください
import re
import sys
import numpy as np
from pathlib import Path

log_path = Path(sys.argv[1] if len(sys.argv) > 1 else "logs/pfpl/diff_btc.log")

text = log_path.read_text(encoding="utf-8", errors="ignore")  # ★ ここを追加
vals = [float(m.group(1)) for m in re.finditer(r"\[DIFF_SIGN\]\s+([\-0-9.]+)", text)]

a = np.array(vals)
print(f"positive % : {np.mean(a > 0)*100:.2f}")
print(f"negative % : {np.mean(a < 0)*100:.2f}")
print(f"max diff   : {a.max():.4f}")
