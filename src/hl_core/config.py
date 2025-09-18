from __future__ import annotations

import os
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator


def load_env_file() -> None:
    load_dotenv(override=True)


class Settings(BaseModel):
    dry_run: bool = True
    database_url: str = "sqlite:///bot.db"
    network: Literal["mainnet", "testnet"] = "testnet"
    account_address: Optional[str] = None
    private_key: Optional[str] = None
    log_level: str = "INFO"

    @field_validator("dry_run", mode="before")
    def _coerce_bool(cls, v):
        if isinstance(v, bool):
            return v
        if v is None:
            return True
        return str(v).strip().lower() in {"1", "true", "yes", "on"}

    @field_validator("network", mode="before")
    def _norm_network(cls, v):
        if not v:
            return "testnet"
        s = str(v).strip().lower()
        if s in {"mainnet", "main", "prod", "production"}:
            return "mainnet"
        return "testnet"


def load_settings() -> Settings:
    load_env_file()
    raw: dict[str, Any] = {
        "dry_run": os.getenv("DRY_RUN", "true"),
        "database_url": os.getenv("DATABASE_URL", "sqlite:///bot.db"),
        "network": os.getenv("NETWORK", "testnet"),
        "account_address": os.getenv("HL_ACCOUNT_ADDRESS"),
        "private_key": os.getenv("HL_PRIVATE_KEY"),
        "log_level": os.getenv("LOG_LEVEL", "INFO"),
    }
    return Settings.model_validate(raw)


def require_live_creds(settings: Settings) -> None:
    if not settings.dry_run:
        if not settings.private_key or not settings.account_address:
            raise ValueError(
                "Live mode requires HL_PRIVATE_KEY and HL_ACCOUNT_ADDRESS in .env"
            )


def mask_secret(value: Optional[str], show: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= show * 2:
        return "*" * len(value)
    return value[:show] + "…" + value[-show:]
