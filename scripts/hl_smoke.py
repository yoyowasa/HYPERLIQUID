"""
Hyperliquid API 疎通スモークテスト（非破壊）

- REST: Info.meta(), user_state(address)
- WS  : l2Book(best bid/ask) を1件だけ受信
- SDK : Wallet/Exchange 初期化のみ（発注はしない）

使い方:
  .venv\\Scripts\\python.exe scripts\\hl_smoke.py --symbol ETH-PERP

注意:
- 発注は一切行いません（完全に非破壊）。
- .env を自動読込します（NETWORK/HL_ACCOUNT_ADDRESS/HL_PRIVATE_KEY 等）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

# .env 読込（python-dotenv 不要の互換レイヤ）
from hl_core.utils.dotenv_compat import load_dotenv  # noqa: E402
from hl_core.config import load_settings  # noqa: E402


def _short(addr: str | None) -> str:
    """アドレスを短縮表示する補助。"""

    a = addr or ""
    return a if len(a) <= 12 else f"{a[:8]}…{a[-4:]}"


async def _rest_checks(symbol: str) -> dict[str, Any]:
    """REST 疎通: meta()/user_state() を確認。"""

    # 公式 SDK を優先
    try:  # pragma: no cover
        from hyperliquid.info import Info  # type: ignore
        from hyperliquid.utils import constants as C  # type: ignore
    except Exception:  # pragma: no cover - オフライン開発時のスタブ
        from hyperliquid_stub.info import Info  # type: ignore
        from hyperliquid_stub.utils import constants as C  # type: ignore

    s = load_settings()
    base = C.MAINNET_API_URL if (s.network or "").lower() == "mainnet" else C.TESTNET_API_URL
    info: Any = Info(base, skip_ws=True)

    meta_raw: Any = info.meta()
    meta: dict[str, Any] = meta_raw or {}
    if not isinstance(meta, dict):
        meta = {}
    uni = meta.get("universe") or []
    if not isinstance(uni, list):
        uni = []
    coin = (symbol or "ETH-PERP").split("-", 1)[0]

    user = None
    if s.account_address:
        user_fn = getattr(info, "user_state", None)
        if callable(user_fn):
            try:
                user = user_fn(s.account_address)
            except Exception as e:  # pragma: no cover
                user = {"error": str(e)}

    # サンプルを出す（なければ先頭、なければ空dict）
    sample = next((u for u in uni if isinstance(u, dict) and u.get("name") == coin), None)
    if sample is None:
        sample = uni[0] if uni else {}

    return {
        "base_url": base,
        "universe_size": len(uni),
        "sample": sample,
        "user_state": user,
    }


async def _ws_check(symbol: str) -> dict[str, Any]:
    """WS 疎通: l2Book を1件だけ受信。"""

    from hl_core.api.ws import subscribe_level2

    out: dict[str, Any] = {}
    try:
        async def _one():
            async for msg in subscribe_level2(symbol):
                return msg
        msg = await asyncio.wait_for(_one(), timeout=10.0)
        out = {
            "channel": msg.get("channel", "l2Book"),
            "coin": msg.get("coin"),
            "best_bid": msg.get("best_bid"),
            "best_ask": msg.get("best_ask"),
        }
    except Exception as e:  # pragma: no cover - ネットワーク事情
        out = {"error": str(e)}
    return out


async def _sdk_init() -> dict[str, Any]:
    """SDK（Wallet/Exchange）の初期化のみ確認（発注はしない）。"""

    # eth_account は任意依存のためフォールバックあり
    try:  # pragma: no cover
        from eth_account.account import Account  # type: ignore
    except Exception:  # pragma: no cover
        Account = None  # type: ignore

    # 公式 SDK を優先
    try:  # pragma: no cover
        from hyperliquid.exchange import Exchange  # type: ignore
        from hyperliquid.utils import constants as C  # type: ignore
    except Exception:  # pragma: no cover
        from hyperliquid_stub.exchange import Exchange  # type: ignore
        from hyperliquid_stub.utils import constants as C  # type: ignore

    s = load_settings()
    base = C.MAINNET_API_URL if (s.network or "").lower() == "mainnet" else C.TESTNET_API_URL
    addr = s.account_address or ""

    wallet_ok = False
    if s.private_key and Account is not None:
        try:
            w = Account.from_key(s.private_key)
            wallet_ok = True
            if addr and getattr(w, "address", "").lower() != addr.lower():
                return {
                    "exchange": "skipped (address mismatch)",
                    "wallet_address": getattr(w, "address", None),
                    "env_address": addr,
                }
            # Exchange 初期化（ネットワーク I/O は発生しない）
            _ = Exchange(w, base, account_address=addr)
            return {
                "exchange": "ok",
                "wallet_address": getattr(w, "address", None),
                "env_address": addr or None,
            }
        except Exception as e:  # pragma: no cover
            return {"exchange": "failed", "error": str(e)}
    return {"exchange": "skipped (no private_key)", "wallet_ok": wallet_ok}


async def main() -> int:
    # .env 読込
    load_dotenv()

    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="ETH-PERP")
    args = p.parse_args()

    # REST
    rest = await _rest_checks(args.symbol)
    print("[REST]", json.dumps(rest, ensure_ascii=False))

    # WS
    ws = await _ws_check(args.symbol)
    print("[WS]", json.dumps(ws, ensure_ascii=False))

    # SDK init
    sdk = await _sdk_init()
    print("[SDK]", json.dumps(sdk, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
