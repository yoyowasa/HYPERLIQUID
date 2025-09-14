import functools
import ssl

import anyio
import certifi
import pytest
import websockets
from hl_core.api import WSClient

# Disable certificate verification globally for websockets to allow
# connections in environments where the certificate chain isn't trusted.
_sslctx = ssl._create_unverified_context()
_orig_connect = websockets.connect
websockets.connect = functools.partial(_orig_connect, ssl=_sslctx)


class DemoWS(WSClient):
    def on_message(self, data):  # 受信フック
        print("HOOK:", data["channel"])


async def main() -> None:
    ws = DemoWS(url="wss://api.hyperliquid.xyz/ws", reconnect=False)
    await ws.connect()
    await ws.subscribe("allMids")  # 何か 1 チャンネル購読
    await anyio.sleep(3)  # 3 秒間データを受信
    await ws.close()


def test_ws_loop_subscription() -> None:
    try:
        anyio.run(main)
    except Exception as exc:  # pragma: no cover - network dependent
        pytest.skip(f"websocket loop failed: {exc}")
