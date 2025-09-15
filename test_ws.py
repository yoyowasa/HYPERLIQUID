"""Tests for WebSocket subscription with a local mock server (no network)."""
from __future__ import annotations

import json
from threading import Thread

import anyio
import pytest
import websockets
from websockets.sync.server import serve


# mock_hyperliquid_ws_server: Hyperliquid風の応答を返すローカルWSサーバを起動する
@pytest.fixture
def mock_hyperliquid_ws_server() -> str:
    """Spin up a local WebSocket server that mimics Hyperliquid responses."""
    def handler(ws) -> None:  # pragma: no cover - exercised indirectly
        # クライアントからsubscribe要求を受信し、ACKを返した後、3件のイベントを送る
        raw = ws.recv()
        request = json.loads(raw)
        ws.send(json.dumps({"channel": "subscriptionResponse", "data": request}))
        for i in range(3):
            ws.send(json.dumps({"channel": "allMids", "data": {"sequence": i}}))

    server = serve(handler, "localhost", 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.socket.getsockname()[1]
    try:
        yield f"ws://localhost:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=1)


# _subscriber: モックWSへ接続し、ACKと3件のイベントを検証する
async def _subscriber(url: str) -> None:
    """Connect to the mock WS and assert ACK + 3 sequential allMids messages."""
    async with websockets.connect(url, ping_interval=None) as ws:
        await ws.send(
            json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
        )

        ack = await ws.recv()
        ack_payload = json.loads(ack)
        assert ack_payload.get("channel") == "subscriptionResponse"

        for expected_sequence in range(3):
            msg = await ws.recv()
            payload = json.loads(msg)
            assert payload == {
                "channel": "allMids",
                "data": {"sequence": expected_sequence},
            }


# test_ws_subscription_mock: anyioで非同期購読を実行するローカル専用テスト
def test_ws_subscription_mock(mock_hyperliquid_ws_server: str) -> None:
    """Runs the subscriber against a local mock server only (no network)."""
    anyio.run(_subscriber, mock_hyperliquid_ws_server)

