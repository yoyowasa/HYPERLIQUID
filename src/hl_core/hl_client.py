from __future__ import annotations

from typing import Optional, Tuple, Any

# eth_account は開発/CI では常備されていないことがあるため、
# ここではフォールバックを用意してインポート失敗時でもインポート可能にする。
try:  # pragma: no cover - 実行環境依存
    from eth_account import Account  # type: ignore
    from eth_account.signers.local import LocalAccount  # type: ignore
except Exception:  # pragma: no cover - フォールバック分岐
    class _DummyLocalAccount:  # 最小限: address プロパティのみ
        def __init__(self, addr: str) -> None:
            self.address = addr

    class Account:  # type: ignore
        @staticmethod
        def from_key(key: str):
            # 疑似的に 0x + 先頭/末尾を残したダミーアドレスを返す
            head = (key or "").replace("0x", "")[:8]
            addr = "0x" + (head or "DEADBEEF").ljust(40, "0")
            return _DummyLocalAccount(addr)

    LocalAccount = _DummyLocalAccount  # type: ignore

# Prefer official SDK, fall back to local stubs in tests/offline
try:  # pragma: no cover - import path shim
    import os as _os
    if _os.getenv("PYTEST_CURRENT_TEST"):
        raise ImportError("force stubs during tests")
    from hyperliquid.exchange import Exchange  # type: ignore
    from hyperliquid.info import Info  # type: ignore
    from hyperliquid.utils import constants  # type: ignore
except Exception:  # pragma: no cover - fallback
    from hyperliquid_stub.exchange import Exchange  # type: ignore
    from hyperliquid_stub.info import Info  # type: ignore
    from hyperliquid_stub.utils import constants  # type: ignore

from .config import Settings, mask_secret, require_live_creds


def get_base_url(network: str) -> str:
    return (
        constants.MAINNET_API_URL if network == "mainnet" else constants.TESTNET_API_URL
    )


def make_account(settings: Settings) -> Optional[Any]:  # LocalAccount は動的 import のため型は Any に後退
    if not settings.private_key:
        return None
    return Account.from_key(settings.private_key)


def make_clients(settings: Settings) -> Tuple[Any, Optional[Any], str]:
    base_url = get_base_url(settings.network)
    info = Info(base_url, skip_ws=True)
    account = make_account(settings)

    resolved_address = (
        settings.account_address or (account.address if account else "")
    ) or ""

    if (
        account
        and settings.account_address
        and settings.account_address.lower() != account.address.lower()
    ):
        print(
            f"[warn] HL_ACCOUNT_ADDRESS と秘密鍵由来のアドレスが不一致です。"
            f" 使用候補: {account.address} (pk={mask_secret(settings.private_key)})"
        )

    exchange = Exchange(account, base_url) if account else None
    return info, exchange, resolved_address


def safe_market_open(
    exchange: Exchange, coin: str, is_buy: bool, sz: float, settings: Settings
):
    if settings.dry_run:
        print(
            f"[dry-run] market_open({coin}, buy={is_buy}, sz={sz}) はブロックされました（DRY_RUN=true）"
        )
        return {"status": "dry-run"}
    require_live_creds(settings)
    market_open = getattr(exchange, "market_open", None)
    if not callable(market_open):  # pragma: no cover - defensive guard
        raise AttributeError("exchange.market_open is unavailable")
    return market_open(coin, is_buy, sz)
