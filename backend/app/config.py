"""
Centralized, strictly-typed application configuration.

Everything the app reads from the environment goes through this module
so a missing/misconfigured variable fails loudly at startup rather than
as a mysterious None deep inside an adapter.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Server ---------------------------------------------------------
    environment: str = Field(default="development")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- Supabase ---------------------------------------------------------
    supabase_url: AnyHttpUrl
    supabase_service_role_key: str = Field(repr=False)
    supabase_jwt_secret: str = Field(repr=False)

    # --- Provider credentials ---------------------------------------------
    openrouter_api_key: str = Field(repr=False)
    groq_api_key: str = Field(repr=False)

    # --- Racing engine tuning ----------------------------------------------
    # Hard ceiling on how long we wait for *any* provider to produce a
    # first token before we give up on the whole race.
    race_timeout_seconds: float = Field(default=20.0, gt=0)
    # Per-provider connect/read timeouts for the underlying httpx clients.
    provider_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    provider_read_timeout_seconds: float = Field(default=30.0, gt=0)

    # --- Circuit breaker -----------------------------------------------------
    circuit_breaker_failure_threshold: int = Field(default=3, gt=0)
    circuit_breaker_reset_seconds: float = Field(default=60.0, gt=0)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings are process-wide and immutable after boot — cache the singleton."""
    return Settings()  # type: ignore[call-arg]  # populated from env/.env
