import re
import sys
import numpy as np
from pathlib import Path

vals = []
pat = re.compile(r"\[DIFF\]\s+([0-9.]+)")
for line in Path(sys.argv[1]).read_text().splitlines():
    m = pat.search(line)
    if m:
        vals.append(float(m.group(1)))

a = np.array(vals)
for p in (50, 60, 70, 80, 90, 95):
    print(f"{p}th percentile : {np.percentile(a, p):.4f}")
print(f"max               : {a.max():.4f}")
