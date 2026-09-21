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
    # The privileged, RLS-bypassing key used for every server-side write
    # (background message logging, semantic cache, admin stats). Accepts
    # Supabase's current `sb_secret_...` secret key (recommended — see
    # `SUPABASE_SECRET_KEY` in .env.example) or, for older projects still
    # on the legacy format, the `service_role` JWT. Whichever format this
    # is, it is sent on the `apikey` header only (see
    # `app/services/supabase_logger.py`) — the new secret keys are not
    # JWTs and get rejected if also sent as `Authorization: Bearer`.
    supabase_secret_key: str = Field(repr=False)
    # JWKS endpoint for verifying user session JWTs — required for current
    # Supabase projects (Auth now issues ES256/RS256-signed tokens by
    # default; Project Settings -> JWT Signing Keys -> JWKS URL). Format:
    # https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json
    supabase_jwks_url: str | None = Field(default=None)
    # Legacy fallback: the single shared HS256 secret from projects that
    # haven't migrated to JWT signing keys (Project Settings -> API -> JWT
    # Secret). Only used when `supabase_jwks_url` is not set.
    supabase_jwt_secret: str | None = Field(default=None, repr=False)

    @field_validator("supabase_jwt_secret")
    @classmethod
    def _require_one_jwt_verification_method(cls, value: str | None, info) -> str | None:
        # Pydantic v2 validates fields in declaration order, so
        # `supabase_jwks_url` is already available in `info.data` here.
        if not value and not info.data.get("supabase_jwks_url"):
            raise ValueError(
                "Set either SUPABASE_JWKS_URL (current Supabase projects) or "
                "SUPABASE_JWT_SECRET (legacy HS256 projects) so incoming "
                "session tokens can be verified."
            )
        return value

    # --- Provider credentials ---------------------------------------------
    # Required (the two reference adapters). The other four are optional —
    # omit the key and that provider is simply left out of the race.
    openrouter_api_key: str = Field(repr=False)
    groq_api_key: str = Field(repr=False)
    cerebras_api_key: str | None = Field(default=None, repr=False)
    mistral_api_key: str | None = Field(default=None, repr=False)
    google_ai_studio_api_key: str | None = Field(default=None, repr=False)
    cloudflare_account_id: str | None = Field(default=None, repr=False)
    cloudflare_api_token: str | None = Field(default=None, repr=False)

    # --- Circuit breaker persistence (optional — Upstash Redis free tier) ---
    # When both are set, breaker state is shared/restart-safe via Upstash's
    # REST API. When omitted, falls back to an in-memory breaker — still
    # fully functional on a single free-tier instance, just not persistent
    # across redeploys.
    upstash_redis_rest_url: str | None = Field(default=None)
    upstash_redis_rest_token: str | None = Field(default=None, repr=False)

    # --- Semantic response cache (optional — needs pgvector + fastembed) ----
    semantic_cache_enabled: bool = Field(default=True)
    semantic_cache_similarity_threshold: float = Field(default=0.94, ge=0.0, le=1.0)

    # --- Observability (optional) --------------------------------------------
    sentry_dsn: str | None = Field(default=None, repr=False)

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
