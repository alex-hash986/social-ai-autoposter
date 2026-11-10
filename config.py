"""Typed application configuration loaded from the project .env file."""

import os
from pathlib import Path
from secrets import token_hex

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr

PROJECT_ROOT = Path(__file__).resolve().parent


class Settings(BaseModel):
    """Store credentials and application settings without requiring API keys at startup."""

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    telegram_bot_token: SecretStr | None = None
    telegram_channel_id: str | None = None
    vk_access_token: SecretStr | None = None
    vk_group_id: str | None = None
    secret_key: SecretStr = Field(default_factory=lambda: SecretStr(token_hex(32)))


def load_settings() -> Settings:
    """Read nonempty settings, giving existing environment variables precedence."""
    load_dotenv(PROJECT_ROOT / ".env")
    return Settings.model_validate(
        {
            name: value
            for name in Settings.model_fields
            if (value := os.environ.get(name.upper()))
        }
    )
