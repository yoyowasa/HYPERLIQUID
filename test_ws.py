import json
import ssl

import anyio
import certifi
import pytest
import websockets


async def main() -> None:
    # Disable certificate verification to allow connecting in environments
    # where the Hyperliquid certificate chain is not trusted.
    _sslctx = ssl._create_unverified_context()

    async with websockets.connect(
        "wss://api.hyperliquid.xyz/ws", ping_interval=None, ssl=_sslctx
    ) as ws:
        await ws.send(
            json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
        )

        for _ in range(3):  # ここを追加 —— 3 件だけ受信
            msg = await ws.recv()
            print("recv:", msg[:200], "…")


def test_ws_subscription() -> None:
    try:
        anyio.run(main)
    except Exception as exc:  # pragma: no cover - network dependent
        pytest.skip(f"websocket connection failed: {exc}")
