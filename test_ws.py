"""Tests for WebSocket subscription using both mock and live servers."""

from __future__ import annotations

import json
import ssl
from threading import Thread

import anyio
import certifi
import pytest
import websockets
from websockets.sync.server import serve


@pytest.fixture
def mock_hyperliquid_ws_server() -> str:
    """Spin up a local WebSocket server that mimics Hyperliquid responses."""

    def handler(ws) -> None:  # pragma: no cover - exercised indirectly
        raw = ws.recv()
        request = json.loads(raw)
        ws.send(
            json.dumps(
                {
                    "channel": "subscriptionResponse",
                    "data": request,
                }
            )
        )
        for i in range(3):
            ws.send(
                json.dumps(
                    {
                        "channel": "allMids",
                        "data": {"sequence": i},
                    }
                )
            )

    server = serve(handler, "localhost", 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.socket.getsockname()[1]
    try:
        yield f"ws://localhost:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=1)


async def _subscriber(url: str) -> None:
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


async def _collect_live_all_mids() -> list[dict[str, object]]:
    """Subscribe to the live Hyperliquid WS and collect a few allMids messages."""

    sslctx = ssl.create_default_context(cafile=certifi.where())

    async with anyio.fail_after(5):
        async with websockets.connect(
            "wss://api.hyperliquid.xyz/ws", ping_interval=None, ssl=sslctx
        ) as ws:
            await ws.send(
                json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
            )
            messages: list[dict[str, object]] = []
            while len(messages) < 3:
                raw = await ws.recv()
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("channel") == "allMids":
                    messages.append(data)
            return messages


def test_ws_subscription_mock(mock_hyperliquid_ws_server: str) -> None:
    anyio.run(_subscriber, mock_hyperliquid_ws_server)


@pytest.mark.network
def test_ws_subscription_live() -> None:
    try:
        messages = anyio.run(_collect_live_all_mids)
    except Exception as exc:  # pragma: no cover - network dependent
        pytest.skip(f"websocket connection failed: {exc}")
    assert len(messages) == 3
    for msg in messages:
        assert msg.get("channel") == "allMids"
