import os
import httpx
import logging

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
LOGGER = logging.getLogger(__name__)


async def discord_notify(msg: str) -> None:
    """
    Send text message to Discord via Incoming Webhook.
    """
    if not WEBHOOK:
        LOGGER.debug("Discord webhook not set; skip notify: %s", msg)
        return
    try:
        payload = {"content": msg}
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.post(WEBHOOK, json=payload)
    except Exception as e:
        LOGGER.warning("Discord notify error: %s", e)
