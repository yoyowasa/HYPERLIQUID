# tests/unit/test_pfpl_init.py
from asyncio import Semaphore
from bots.pfpl.strategy import PFPLStrategy


def test_init(monkeypatch):
    # ── ダミー鍵 (32 byte hex) を環境変数にセット ──
    monkeypatch.setenv("HL_ACCOUNT_ADDR", "0xTEST")
    monkeypatch.setenv("HL_API_SECRET", "0x" + "11" * 32)

    # ── セマフォは 1 で十分 ──
    sem = Semaphore(1)

    # 例外が出なければ成功
    PFPLStrategy(config={}, semaphore=sem)
