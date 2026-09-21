"""
Provider-agnostic adapter contract.

Every inference provider (OpenRouter, Groq, and whatever gets added later —
Cerebras, Mistral, Google AI Studio, HF Serverless, ...) implements this
single ABC. The racing engine (`app.core.racing_engine`) only ever talks to
`BaseModelAdapter`, so adding a 7th provider is a matter of writing one new
file and registering it — the engine, the SSE layer, and the frontend never
change.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from app.models.schemas import ChatMessage


class ProviderError(Exception):
    """Raised by an adapter for any failure that should trigger failover.

    `retryable` tells the circuit breaker whether this failure counts
    against the provider (429/5xx/timeout) or was a hard client error
    (400 — bad request shape) that retrying elsewhere won't fix either,
    but which should still not be silently swallowed.
    """

    def __init__(self, provider: str, message: str, *, status_code: int | None = None, retryable: bool = True) -> None:
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(f"[{provider}] {message}")


@dataclass(slots=True)
class StreamChunk:
    """A single normalized delta emitted by an adapter's stream() generator."""

    delta: str
    is_first_token: bool = False
    finish_reason: str | None = None
    # Wall-clock ms from request start to this chunk — populated by the
    # racing engine, not the adapter, so it stays comparable across providers.
    elapsed_ms: int = 0


@dataclass(slots=True)
class ProviderConfig:
    """Everything an adapter needs to construct its httpx.AsyncClient."""

    api_key: str
    base_url: str
    model: str
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Whether `model` accepts image_url content parts. Set per-request in
    # main.py's adapter factory (a vision-capable model is swapped in when
    # the incoming message list contains images) rather than hardcoded per
    # provider class, since most providers offer both text-only and vision
    # variants of the same family.
    supports_vision: bool = False


class BaseModelAdapter(ABC):
    """
    Strictly-typed contract for a streaming chat-completion provider.

    Subclasses own exactly two responsibilities:
      1. Translate the normalized `ChatMessage` list + params into the
         provider's wire format (`_build_payload`).
      2. Parse the provider's raw SSE/byte stream into normalized
         `StreamChunk`s (`stream`).

    Everything else — timing, cancellation, circuit-breaking, failover — is
    the racing engine's job, not the adapter's.
    """

    #: Human-readable identifier used in telemetry and logs. Must be unique
    #: across all registered adapters.
    provider_name: str

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "BaseModelAdapter":
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=httpx.Timeout(
                connect=self.config.connect_timeout_seconds,
                read=self.config.read_timeout_seconds,
                write=self.config.connect_timeout_seconds,
                pool=self.config.connect_timeout_seconds,
            ),
            headers=self._build_headers(),
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                f"{self.provider_name} adapter used outside its `async with` context."
            )
        return self._client

    @abstractmethod
    def _build_headers(self) -> dict[str, str]:
        """Provider-specific auth/content headers."""

    @abstractmethod
    def _build_payload(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
    ) -> dict:
        """Translate normalized messages into the provider's request body."""

    @abstractmethod
    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[StreamChunk]:
        """
        Open the provider's streaming endpoint and yield normalized chunks.

        Implementations MUST raise `ProviderError` (never let raw httpx/JSON
        exceptions escape) so the racing engine can treat every entrant
        uniformly.
        """

    @staticmethod
    def _now_ms(start_perf_counter: float) -> int:
        return int((time.perf_counter() - start_perf_counter) * 1000)
