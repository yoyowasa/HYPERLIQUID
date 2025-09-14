import json

import ssl

import anyio
import certifi
import pytest
import websockets

# Tests use certifi's CA bundle so websocket connections verify server
# certificates instead of disabling SSL verification.

# 検証済みのSSLコンテキストでHyperliquid WSに接続し、
# "allMids" チャネルの実データだけを3件集めて返す（確認用の軽量ヘルパ）
async def main() -> list[dict[str, object]]:

    sslctx = ssl.create_default_context(cafile=certifi.where())

    async with anyio.fail_after(5):
        async with websockets.connect(
            "wss://api.hyperliquid.xyz/ws", ping_interval=None, ssl=sslctx
        ) as ws:
            await ws.send(
                json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
            )
            messages: list[dict[str, object]] = []
            while len(messages) < 3:
                raw = await ws.recv()
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("channel") == "allMids":
                    messages.append(data)
            return messages



def test_ws_subscription() -> None:
    try:
        messages = anyio.run(main)
    except Exception as exc:  # pragma: no cover - network dependent
        pytest.skip(f"websocket connection failed: {exc}")
    assert len(messages) == 3
    for msg in messages:
        assert msg.get("channel") == "allMids"
