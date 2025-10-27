from __future__ import annotations

from typing import Optional, Tuple, Any

from eth_account import Account
from eth_account.signers.local import LocalAccount

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


def make_account(settings: Settings) -> Optional[LocalAccount]:
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
