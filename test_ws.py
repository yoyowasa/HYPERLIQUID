import json
import ssl

import anyio
import certifi
import websockets


async def main():
    # Use certifi's CA bundle to validate the WebSocket TLS handshake.
    _sslctx = ssl.create_default_context(cafile=certifi.where())

    async with websockets.connect(
        "wss://api.hyperliquid.xyz/ws", ping_interval=None, ssl=_sslctx
    ) as ws:
        await ws.send(
            json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
        )

        for _ in range(3):  # ここを追加 —— 3 件だけ受信
            msg = await ws.recv()
            print("recv:", msg[:200], "…")


anyio.run(main)
