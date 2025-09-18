# function: entrypoint (hello bot)
# function: これは「接続確認だけ」をする超安全なサンプルです。注文は一切出しません（DRY_RUNに関係なく発注ロジック不使用）。

from __future__ import annotations
import json
from typing import Any

from hl_core.config import load_settings  # function: .env の読み込み＋型付き設定
from hl_core.hl_client import make_clients  # function: Info/Exchange の初期化（安全ガード込み）

# function: 文字列を短くマスク表示する（例: 0x1234…ABCD）
def _short(addr: str, head: int = 6, tail: int = 4) -> str:
    # function: アドレスが短い/空ならそのまま返す
    if not addr or len(addr) <= head + tail:
        return addr or "(unset)"
    return f"{addr[:head]}…{addr[-tail:]}"

# function: JSONを見やすく出す（日本語も崩れない）
def _pjson(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))

# function: メイン処理。設定読込→SDK初期化→ユーザ状態を取得（アドレス未設定ならスキップ）
def main() -> None:
    settings = load_settings()
    info, exchange, address = make_clients(settings)

    print(f"[hello] network={settings.network} dry_run={settings.dry_run} address={_short(address)}")

    # function: アドレスが設定されていれば user_state を取得して表示（公開APIなので発注はしない）
    if address:
        try:
            state = info.user_state(address)  # function: SDKの例にある基本API（README準拠）
            # function: 中身が多いので、最初は主キーだけ見せて全体は折りたたみ表示
            print(f"[hello] user_state keys: {list(state.keys())[:8]} ...")
            _pjson(state)
        except Exception as e:
            print(f"[warn] failed to fetch user_state: {e}")
    else:
        print("[hello] ユーザ状態の取得をスキップしました。`.env` に HL_ACCOUNT_ADDRESS を設定すると表示できます。")

    # function: Exchange は作成済みでも、このサンプルでは一切注文しない
    if exchange is None:
        print("[hello] exchange=None（鍵未設定 or 署名無効）。発注機能は作られていません。")
    else:
        print("[hello] exchange=ready（ただしこのサンプルは発注しません）")

    print("[hello] done. (no orders)")

if __name__ == "__main__":
    main()
