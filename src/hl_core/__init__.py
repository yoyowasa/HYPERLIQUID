# --- Python 3.13 互換パッチ（parsimonious 用） -----------------
import inspect

if not hasattr(inspect, "getargspec"):  # 3.13 以降でだけ実行
    inspect.getargspec = inspect.getfullargspec  # type: ignore[attr-defined]
# ----------------------------------------------------------------
