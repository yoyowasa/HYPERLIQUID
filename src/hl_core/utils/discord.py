import logging
import httpx
import asyncio


class DiscordHandler(logging.Handler):
    """ERROR 以上を Discord に POST"""

    def __init__(self, webhook: str, level: int = logging.ERROR) -> None:
        super().__init__(level)
        self.webhook = webhook
        self._client = httpx.AsyncClient(timeout=5)

    async def _post(self, content: str) -> None:
        try:
            await self._client.post(self.webhook, json={"content": content})
        except Exception as exc:  # noqa: BLE001
            # Discord 送信失敗はローカル WARNING に留める
            logging.getLogger("hl_core.utils.discord").warning(
                "discord post fail: %s", exc
            )

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        msg = self.format(record)
        asyncio.create_task(self._post(f"```{msg}```"))
