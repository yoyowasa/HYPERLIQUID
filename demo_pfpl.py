import anyio
from hl_core.api import WSClient
from bots.pfpl import PFPLStrategy
from hl_core.utils.dotenv_compat import load_dotenv

load_dotenv()

strategy = PFPLStrategy(config={})


async def main() -> None:
    ws = WSClient("wss://api.hyperliquid.xyz/ws", reconnect=True)
    ws.on_message = strategy.on_message  # ← Strategy の hook を差し込む
    await ws.connect()
    await ws.subscribe("allMids")
    await anyio.sleep(5)  # 5 秒だけ受信
    await ws.close()


anyio.run(main)
