from bots.pfpl import PFPLStrategy

def test_init(monkeypatch):
    # ダミー鍵を環境変数にセット
    monkeypatch.setenv("HL_ACCOUNT_ADDR", "0xTEST")
    monkeypatch.setenv("HL_API_SECRET", "dummysecret")
    # 例外が出なければ成功
    PFPLStrategy(config={})
