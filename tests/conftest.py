# anyio
# tests/conftest.py（ファイル先頭に追加）
import os
import importlib

if os.getenv("OFFLINE_STUBS") == "1":
    importlib.import_module("sitecustomize")  # ルート or src どちらでもOK

# anyio の可用性を判定してテスト分岐に使う（未使用インポート回避・インデント崩れ防止）
from importlib import util as _importlib_util

ANYIO_AVAILABLE = _importlib_util.find_spec("anyio") is not None


# colorama (no-op)
try:
    import colorama  # type: ignore
except ImportError:
    import types
    import sys

    colorama = types.SimpleNamespace(
        Fore=types.SimpleNamespace(RED="", GREEN="", YELLOW="", RESET=""),
        Style=types.SimpleNamespace(RESET_ALL=""),
    )

    def init(*args, **kwargs):
        pass

    colorama.init = init
    sys.modules["colorama"] = colorama

# hyperliquid.exchange が利用可能かどうかを検出して、テストの条件分岐に使う（未使用インポート回避）

# hyperliquid.exchange の可用性を、import 文を使わずに判定してE402を回避する
HYPERLIQUID_EXCHANGE_AVAILABLE = (
    __import__("importlib").util.find_spec("hyperliquid.exchange") is not None
)
