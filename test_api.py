import os

import anyio
import certifi
from hl_core.api import HTTPClient

# Ensure HTTPClient uses certifi's CA bundle for SSL verification.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())


async def main() -> None:
    cli = HTTPClient(base_url="https://api.hyperliquid.xyz")
    data = await cli.post("info", data={"type": "meta"})  # 実在パスに合わせて
    print("sample keys:", list(data)[:3])
    await cli.close()


anyio.run(main)
