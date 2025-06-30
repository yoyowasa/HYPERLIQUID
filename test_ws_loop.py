import anyio
from hl_core.api import WSClient


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
