import json
import ssl

import anyio
import pytest
import websockets


async def main() -> list[dict]:
    # Disable certificate verification to allow connecting in environments
    # where the Hyperliquid certificate chain is not trusted.
    _sslctx = ssl._create_unverified_context()

    async with websockets.connect(
        "wss://api.hyperliquid.xyz/ws", ping_interval=None, ssl=_sslctx
    ) as ws:
        await ws.send(
            json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
        )

        messages: list[dict] = []
        for _ in range(3):
            msg = await ws.recv()
            messages.append(json.loads(msg))
    return messages


def test_ws_subscription() -> None:
    try:
        messages = anyio.run(main)
    except Exception as exc:  # pragma: no cover - network dependent
        pytest.skip(f"websocket connection failed: {exc}")
    assert len(messages) == 3
    for msg in messages:
        assert msg.get("channel") == "allMids"
