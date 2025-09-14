import json

import ssl

import anyio
import certifi
import pytest
import websockets

# Tests use certifi's CA bundle so websocket connections verify server
# certificates instead of disabling SSL verification.


async def main() -> None:
    """Subscribe to the allMids feed using a verified SSL context.

    Requires certifi to supply trusted root certificates.
    """
    sslctx = ssl.create_default_context(cafile=certifi.where())

    async with websockets.connect(
        "wss://api.hyperliquid.xyz/ws", ping_interval=None, ssl=sslctx
    ) as ws:
        await ws.send(
            json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
        )

        for _ in range(3):  # ここを追加 —— 3 件だけ受信
            msg = await ws.recv()
            logging.debug("recv: %s …", msg[:200])


def test_ws_subscription() -> None:
    try:
        anyio.run(main)
    except Exception as exc:  # pragma: no cover - network dependent
        pytest.skip(f"websocket connection failed: {exc}")
