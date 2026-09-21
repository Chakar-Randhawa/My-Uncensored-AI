"""Typed request/response contracts for the chat API and internal racing engine."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ProviderStatus(str, Enum):
    WON = "won"
    CANCELLED = "cancelled"
    ERROR = "error"
    TIMEOUT = "timeout"


class ChatRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(BaseModel):
    role: ChatRole
    content: str = Field(min_length=1, max_length=32_000)
    # Data URLs (`data:image/png;base64,...`) or plain https image URLs.
    # Only meaningful on `role="user"` messages and only forwarded to
    # providers/models that actually support vision input — see
    # `BaseModelAdapter.supports_vision` and each adapter's payload
    # builder for how a non-vision provider degrades (images silently
    # dropped rather than the request failing outright).
    images: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("images")
    @classmethod
    def _validate_image_refs(cls, value: list[str]) -> list[str]:
        for ref in value:
            if not (ref.startswith("data:image/") or ref.startswith("https://")):
                raise ValueError(
                    "images must be data:image/... URLs or https:// URLs, "
                    f"got: {ref[:40]!r}"
                )
        return value


class ChatCompletionRequest(BaseModel):
    """Body of POST /v1/chat/stream."""

    conversation_id: UUID | None = Field(
        default=None,
        description="Existing conversation to append to. Omit to start a new one.",
    )
    messages: list[ChatMessage] = Field(min_length=1)
    # User-controlled, persisted-per-conversation system prompt. Empty by
    # default — the platform does not inject any hidden instructions.
    system_prompt: str | None = Field(default=None, max_length=8_000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=8192)
    # Explicit provider order override. Falls back to the engine default
    # (config-driven) when omitted. A single-element list effectively
    # disables racing and pins the request to one provider (the frontend's
    # provider-selector uses this).
    provider_priority: list[str] | None = None
    # Set false to bypass the semantic cache for this request (e.g. the
    # frontend's explicit "Regenerate" action, which should never return a
    # stale cached answer).
    use_cache: bool = True

    @field_validator("messages")
    @classmethod
    def _last_message_is_user(cls, value: list[ChatMessage]) -> list[ChatMessage]:
        if value[-1].role != ChatRole.USER:
            raise ValueError("The final message in the payload must have role='user'.")
        return value


class ProviderRaceResult(BaseModel):
    """One entrant's outcome in a race — persisted into messages.provider_meta."""

    provider: str
    status: ProviderStatus
    ttft_ms: int | None = None  # time-to-first-token, only set when the stream started
    error: str | None = None


class StreamedAssistantMessage(BaseModel):
    """Assembled server-side once a stream completes, for the background log write."""

    conversation_id: UUID
    user_id: UUID
    content: str
    winning_provider: str
    winning_model: str
    latency_ms: int
    total_duration_ms: int
    token_count: int
    provider_meta: list[ProviderRaceResult]
