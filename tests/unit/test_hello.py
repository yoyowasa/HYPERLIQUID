# function: tests for hello bot

import sys
import types

import pytest


# function: hyperliquid SDKが未インストール環境でもテストできるようダミーモジュールを注入
hyperliquid_pkg = types.ModuleType("hyperliquid")
hyperliquid_exchange = types.ModuleType("hyperliquid.exchange")
hyperliquid_info = types.ModuleType("hyperliquid.info")
hyperliquid_utils = types.ModuleType("hyperliquid.utils")
hyperliquid_utils_constants = types.ModuleType("hyperliquid.utils.constants")


class _DummyInfo:  # function: Info の雛形（user_state/metaのみ利用）
    def __init__(self, *_, **__):
        pass

    def meta(self):
        return {
            "minSizeUsd": {"ETH": "1"},
            "universe": [{"name": "ETH", "pxTick": "0.01"}],
        }

    def user_state(self, *_args, **_kwargs):
        return {"perpPositions": []}


class _DummyExchange:  # function: Exchange の雛形（info/market_openだけ提供）
    def __init__(self, *_, **__):
        self.info = _DummyInfo()

    def market_open(self, *_args, **_kwargs):
        return {"status": "ok"}


# function: テストで利用される定数だけ定義
hyperliquid_utils_constants.MAINNET_API_URL = "https://api.hyperliquid.example"
hyperliquid_utils_constants.TESTNET_API_URL = "https://api.hyperliquid.test"

# function: モジュールを構成してsys.modulesに登録
hyperliquid_exchange.Exchange = _DummyExchange
hyperliquid_info.Info = _DummyInfo
hyperliquid_utils.constants = hyperliquid_utils_constants

sys.modules.setdefault("hyperliquid", hyperliquid_pkg)
sys.modules.setdefault("hyperliquid.exchange", hyperliquid_exchange)
sys.modules.setdefault("hyperliquid.info", hyperliquid_info)
sys.modules.setdefault("hyperliquid.utils", hyperliquid_utils)
sys.modules.setdefault("hyperliquid.utils.constants", hyperliquid_utils_constants)

# function: hyperliquidパッケージからサブモジュールにアクセスできるよう属性も付与
setattr(hyperliquid_pkg, "exchange", hyperliquid_exchange)
setattr(hyperliquid_pkg, "info", hyperliquid_info)
setattr(hyperliquid_pkg, "utils", hyperliquid_utils)

# function: helloボットのmainをテスト対象として読み込む
from bots.hello import main as hello_main
# function: DRY_RUNの挙動テスト用にsafe_market_openを読み込む
from hl_core.hl_client import safe_market_open
# function: 設定オブジェクト（型付き）を利用してダミー設定を作る
from hl_core.config import Settings


# function: main() がアドレスありのときに user_state を表示することを確認（Infoはモック）
def test_main_prints_user_state(monkeypatch, capsys):
    # function: ダミーの設定（testnet/DRY_RUN=true/アドレスあり）
    s = Settings(
        dry_run=True,
        database_url="sqlite:///bot.db",
        network="testnet",
        account_address="0x1234567890abcdef1234567890abcdef12345678",
        private_key=None,
        log_level="INFO",
    )

    # function: user_state を返すだけのダミーInfo
    class DummyInfo:
        # function: 引数のアドレスをそのまま返す簡単なモック
        def user_state(self, addr):
            return {"ok": True, "addr": addr}

    # function: make_clients を差し替え、ネットワーク接続を発生させない
    def stub_make_clients(_settings):
        return DummyInfo(), None, s.account_address

    # function: .env読込を回避し、上のダミー設定を使わせる
    monkeypatch.setattr(hello_main, "load_settings", lambda: s)
    # function: SDK初期化をダミーに差し替え
    monkeypatch.setattr(hello_main, "make_clients", stub_make_clients)

    # function: 実行して出力を取得
    hello_main.main()
    out = capsys.readouterr().out

    # function: 期待する文字列が含まれること（ネットワーク名/DRY_RUN/ダミーJSON）
    assert "network=testnet" in out
    assert "dry_run=True" in out
    assert '"ok": true' in out  # function: JSONにokフラグが含まれること


# function: DRY_RUN=true のときは safe_market_open が実注文を行わず 'dry-run' を返すこと
def test_safe_market_open_dry_run():
    s = Settings(
        dry_run=True,
        database_url="sqlite:///bot.db",
        network="testnet",
        account_address=None,
        private_key=None,
        log_level="INFO",
    )
    # function: DRY_RUNならexchangeはNoneでも到達しない（market_openを呼ばない）
    result = safe_market_open(exchange=None, coin="BTC", is_buy=True, sz=0.001, settings=s)
    assert isinstance(result, dict)
    assert result.get("status") == "dry-run"
