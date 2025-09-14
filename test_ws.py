"""Tests for WebSocket subscription with a mock server."""

from __future__ import annotations

import json
from threading import Thread

import anyio
import pytest
import websockets
from websockets.sync.server import serve


@pytest.fixture
def mock_hyperliquid_ws_server() -> str:
    """Spin up a local WebSocket server that mimics Hyperliquid responses."""

    def handler(ws) -> None:  # pragma: no cover - exercised indirectly
        ws.recv()
        for i in range(3):
            ws.send(json.dumps({"type": "mids", "data": i}))

    server = serve(handler, "localhost", 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.socket.getsockname()[1]
    try:
        yield f"ws://localhost:{port}"
    finally:
        server.shutdown()


async def _subscriber(url: str) -> None:
    async with websockets.connect(url, ping_interval=None) as ws:
        await ws.send(
            json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
        )

        for i in range(3):
            msg = await ws.recv()
            assert json.loads(msg) == {"type": "mids", "data": i}


@pytest.mark.network
def test_ws_subscription(mock_hyperliquid_ws_server: str) -> None:
    anyio.run(_subscriber, mock_hyperliquid_ws_server)
