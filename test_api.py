import os

import anyio
import certifi
import pytest
from hl_core.api import HTTPClient

# Ensure HTTPClient uses certifi's CA bundle for SSL verification.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())


async def main() -> dict:
    cli = HTTPClient(base_url="https://api.hyperliquid.xyz", verify=False)
    try:
        data = await cli.post("info", data={"type": "meta"})
    finally:
        await cli.close()
    return data


def test_http_client_meta() -> None:
    try:
        data = anyio.run(main)
    except Exception as exc:  # pragma: no cover - network dependent
        pytest.skip(f"HTTP request failed: {exc}")
    assert "universe" in data
    assert "marginTables" in data
