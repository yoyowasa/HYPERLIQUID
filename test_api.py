import anyio
from hl_core.api import HTTPClient


async def main() -> None:
    cli = HTTPClient(base_url="https://api.hyperliquid.xyz")
    data = await cli.post("info", data={"type": "meta"})  # 実在パスに合わせて
    print("sample keys:", list(data)[:3])
    await cli.close()


anyio.run(main)
