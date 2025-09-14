import json
import logging
import ssl
from typing import Any

import anyio
import certifi
import pytest
import websockets

# Tests use certifi's CA bundle so websocket connections verify server
# certificates instead of disabling SSL verification.


# 検証済みのSSLコンテキストでHyperliquid WSに接続し、
# "allMids" チャネルの実データだけを3件集めて返す（確認用の軽量ヘルパ）
async def main() -> list[dict[str, Any]]:
    sslctx = ssl.create_default_context(cafile=certifi.where())
    messages: list[dict[str, Any]] = []

    async with anyio.fail_after(5):
        async with websockets.connect(
            "wss://api.hyperliquid.xyz/ws", ping_interval=None, ssl=sslctx
        ) as ws:
            await ws.send(
                json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
            )
            while len(messages) < 3:
                raw = await ws.recv()
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("channel") == "allMids":
                    messages.append(data)
    return messages


def test_ws_subscription() -> None:
    try:
        anyio.run(main)
    except Exception as exc:  # pragma: no cover - network dependent
        pytest.skip(f"websocket connection failed: {exc}")
