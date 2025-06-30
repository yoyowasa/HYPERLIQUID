import json
import anyio
import websockets


async def main():
    async with websockets.connect(
        "wss://api.hyperliquid.xyz/ws", ping_interval=None
    ) as ws:
        await ws.send(
            json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
        )

        for _ in range(3):  # ここを追加 —— 3 件だけ受信
            msg = await ws.recv()
            print("recv:", msg[:200], "…")


anyio.run(main)
