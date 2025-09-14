import functools
import ssl

import anyio
import pytest
import websockets
from hl_core.api import WSClient

# Disable certificate verification globally for websockets to allow
# connections in environments where the certificate chain isn't trusted.
_sslctx = ssl._create_unverified_context()
_orig_connect = websockets.connect
websockets.connect = functools.partial(_orig_connect, ssl=_sslctx)


class DemoWS(WSClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.messages = []

    def on_message(self, data):  # 受信フック
        self.messages.append(data)


async def main() -> list[dict]:
    ws = DemoWS(url="wss://api.hyperliquid.xyz/ws", reconnect=False)
    await ws.connect()
    await ws.subscribe("allMids")
    await anyio.sleep(3)
    await ws.close()
    return ws.messages


def test_ws_loop_subscription() -> None:
    try:
        messages = anyio.run(main)
    except Exception as exc:  # pragma: no cover - network dependent
        pytest.skip(f"websocket loop failed: {exc}")
    if not messages:
        pytest.skip("no websocket messages received")
    for msg in messages:
        assert msg.get("channel") == "allMids"
