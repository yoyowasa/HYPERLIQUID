from __future__ import annotations

from typing import Optional, Tuple

from eth_account import Account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from .config import Settings, mask_secret, require_live_creds


def get_base_url(network: str) -> str:
    return constants.MAINNET_API_URL if network == "mainnet" else constants.TESTNET_API_URL


def make_account(settings: Settings) -> Optional[LocalAccount]:
    if not settings.private_key:
        return None
    return Account.from_key(settings.private_key)


def make_clients(settings: Settings) -> Tuple[Info, Optional[Exchange], str]:
    base_url = get_base_url(settings.network)
    info = Info(base_url, skip_ws=True)
    account = make_account(settings)

    resolved_address = (settings.account_address or (account.address if account else "")) or ""

    if account and settings.account_address and settings.account_address.lower() != account.address.lower():
        print(
            f"[warn] HL_ACCOUNT_ADDRESS と秘密鍵由来のアドレスが不一致です。"
            f" 使用候補: {account.address} (pk={mask_secret(settings.private_key)})"
        )

    exchange = Exchange(account, base_url) if account else None
    return info, exchange, resolved_address


def safe_market_open(exchange: Exchange, coin: str, is_buy: bool, sz: float, settings: Settings):
    if settings.dry_run:
        print(f"[dry-run] market_open({coin}, buy={is_buy}, sz={sz}) はブロックされました（DRY_RUN=true）")
        return {"status": "dry-run"}
    require_live_creds(settings)
    return exchange.market_open(coin, is_buy, sz)
