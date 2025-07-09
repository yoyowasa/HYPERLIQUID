from asyncio import Semaphore
PFPLStrategy(config={}, semaphore=Semaphore(1))


def test_init(monkeypatch):
    # ダミー鍵 (32byte hex) を環境変数にセット
    monkeypatch.setenv("HL_ACCOUNT_ADDR", "0xTEST")
    monkeypatch.setenv("HL_API_SECRET", "0x" + "11" * 32)  # ←ここだけ変更

    # 例外が出なければ成功
    PFPLStrategy(config={})
