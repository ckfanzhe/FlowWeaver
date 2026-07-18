"""Application settings (env-overridable).

The platform's LLM configuration lives in the user-managed `LlmPreset`
table — the `.env.llm` file is reserved for tests. Production code MUST
NOT read LLM keys from environment variables; see `app.core.llm_runner`
for the model constructor that runs strictly off the preset table.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGNOBUILDER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = (
        f"sqlite:///{Path(__file__).parent.parent / 'data' / 'agnobuilder.db'}"
    )

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

settings = Settings()
