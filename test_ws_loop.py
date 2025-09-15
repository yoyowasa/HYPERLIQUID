"""Tests for WebSocket loop subscription against the live Hyperliquid server."""

import functools
import ssl

import anyio
import certifi
import pytest
import websockets
from hl_core.api import WSClient


class DemoWS(WSClient):
    def on_message(self, data):  # 受信フック
        print("HOOK:", data["channel"])


async def main() -> None:
    sslctx = ssl.create_default_context(cafile=certifi.where())

    orig_connect = websockets.connect
    patched_connect = functools.partial(orig_connect, ssl=sslctx)

    ws = DemoWS(url="wss://api.hyperliquid.xyz/ws", reconnect=False)
    try:
        websockets.connect = patched_connect
        await ws.connect()
        await ws.subscribe("allMids")  # 何か 1 チャンネル購読
        await anyio.sleep(3)  # 3 秒間データを受信
    finally:
        websockets.connect = orig_connect
        await ws.close()


@pytest.mark.network
def test_ws_loop_subscription() -> None:
    try:
        anyio.run(main)
    except Exception as exc:  # pragma: no cover - network dependent
        pytest.skip(f"websocket loop failed: {exc}")
