"""Shared test doubles."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.adapters.base import BaseModelAdapter, ProviderConfig, ProviderError, StreamChunk
from app.models.schemas import ChatMessage


class FakeAdapter(BaseModelAdapter):
    """
    A fully controllable fake provider: streams `tokens` after `delay`
    seconds, optionally raising `ProviderError` at a given token index
    (simulating a mid-stream disconnect) or never resolving at all
    (simulating a stalled provider for timeout tests).
    """

    def __init__(
        self,
        name: str,
        *,
        delay: float = 0.0,
        tokens: list[str] | None = None,
        token_interval: float = 0.01,
        fail_at_token: int | None = None,
        never_respond: bool = False,
    ) -> None:
        super().__init__(ProviderConfig(api_key="x", base_url="http://fake", model="fake-model"))
        self.provider_name = name
        self._delay = delay
        self._tokens = tokens or ["Hello"]
        self._token_interval = token_interval
        self._fail_at_token = fail_at_token
        self._never_respond = never_respond
        self.entered = False
        self.exited = False
        self.cancelled = False

    def _build_headers(self) -> dict[str, str]:
        return {}

    def _build_payload(self, messages: list[ChatMessage], *, temperature: float, max_tokens: int) -> dict:
        return {}

    async def __aenter__(self) -> "FakeAdapter":
        self.entered = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.exited = True

    async def stream(
        self, messages: list[ChatMessage], *, temperature: float, max_tokens: int
    ) -> AsyncIterator[StreamChunk]:
        try:
            if self._never_respond:
                await asyncio.sleep(3600)
                return

            await asyncio.sleep(self._delay)

            for i, token in enumerate(self._tokens):
                if self._fail_at_token is not None and i == self._fail_at_token:
                    raise ProviderError(self.provider_name, "simulated mid-stream failure", retryable=True)

                yield StreamChunk(delta=token, is_first_token=(i == 0), elapsed_ms=int(self._delay * 1000))
                await asyncio.sleep(self._token_interval)

        except asyncio.CancelledError:
            self.cancelled = True
            raise
