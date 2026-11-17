"""Settings read from environment variables and an optional `.env` file."""

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default paths are relative to the repository, so the service works from any folder.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    # env_ignore_empty: an empty line like `ANTHROPIC_API_KEY=` in .env counts as not set.
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # No default on purpose: whoever runs the service chooses the real model or the fake,
    # so nobody mistakes one for the other.
    llm_provider: Literal["fake", "anthropic"] | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Effort = "medium"

    operational_db_path: Path = PROJECT_ROOT / "var" / "operational.db"
    analytics_db_path: Path = PROJECT_ROOT / "var" / "analytics.db"
    data_dir: Path = PROJECT_ROOT / "data"
    log_level: str = "INFO"
