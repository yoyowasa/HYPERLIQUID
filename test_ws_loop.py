import functools
import ssl

import anyio
import certifi
import websockets
from hl_core.api import WSClient

# Ensure websockets uses certifi's CA bundle for TLS verification.
_sslctx = ssl.create_default_context(cafile=certifi.where())
_orig_connect = websockets.connect
websockets.connect = functools.partial(_orig_connect, ssl=_sslctx)


class DemoWS(WSClient):
    def on_message(self, data):  # 受信フック
        print("HOOK:", data["channel"])


async def main():
    ws = DemoWS(url="wss://api.hyperliquid.xyz/ws", reconnect=False)
    await ws.connect()
    await ws.subscribe("allMids")  # 何か 1 チャンネル購読
    await anyio.sleep(3)  # 3 秒間データを受信
    await ws.close()


anyio.run(main)
