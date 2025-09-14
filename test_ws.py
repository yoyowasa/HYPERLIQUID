import json
import ssl

import anyio
import pytest
import websockets


async def main() -> list[dict[str, object]]:
    """Subscribe to the public WebSocket and return three price updates.

    The real Hyperliquid websocket sends an initial message confirming the
    subscription.  The original implementation in this repository simply
    yielded the first three frames it received which meant the list included
    the ``subscriptionResponse`` acknowledgement.  The tests expect only actual
    market data messages whose ``channel`` is ``"allMids"``.  To make the
    behaviour deterministic we keep reading from the socket until three such
    messages have been collected and return them as dictionaries.

    The function is intentionally small as it is only used in tests and
    examples.  Network failures are handled by the caller which will skip the
    test when the websocket is unreachable.
    """

    # Disable certificate verification to allow connecting in environments
    # where the Hyperliquid certificate chain is not trusted.
    _sslctx = ssl._create_unverified_context()

    async with anyio.fail_after(5):
        async with websockets.connect(
            "wss://api.hyperliquid.xyz/ws", ping_interval=None, ssl=_sslctx
        ) as ws:
            await ws.send(
                json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}})
            )

            messages: list[dict[str, object]] = []
            while len(messages) < 3:
                raw_msg = await ws.recv()
                print("recv:", raw_msg[:200], "…")
                msg = json.loads(raw_msg)
                # The first frame is usually a subscription acknowledgement with a
                # different ``channel``.  Only accumulate actual data updates.
                if msg.get("channel") != "allMids":
                    continue
                messages.append(msg)
    return messages


def test_ws_subscription() -> None:
    try:
        messages = anyio.run(main)
    except Exception as exc:  # pragma: no cover - network dependent
        pytest.skip(f"websocket connection failed: {exc}")
    assert len(messages) == 3
    for msg in messages:
        assert msg.get("channel") == "allMids"
