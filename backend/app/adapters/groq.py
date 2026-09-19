"""
Groq adapter.

Groq's `/openai/v1/chat/completions` endpoint is OpenAI-compatible and, on
LPU hardware, is typically the fastest entrant in the race by a wide
margin — which is exactly why it's worth racing rather than hardcoding as
"the" provider: Groq's free tier has the tightest rate limits of the pool,
so the circuit breaker needs to open on it aggressively and let the race
fall through to the others.

Groq's final stream chunk carries an `x_groq.usage` block instead of a
separate usage event — we surface it via `finish_reason` on that terminal
chunk so the racing engine can record token counts without a second call.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from app.adapters.base import BaseModelAdapter, ProviderConfig, ProviderError, StreamChunk
from app.models.schemas import ChatMessage


class GroqAdapter(BaseModelAdapter):
    provider_name = "groq"

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
    ) -> dict:
        return {
            "model": self.config.model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[StreamChunk]:
        payload = self._build_payload(messages, temperature=temperature, max_tokens=max_tokens)
        start = time.perf_counter()
        first_token_sent = False

        try:
            async with self.client.stream(
                "POST", "/openai/v1/chat/completions", json=payload
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise ProviderError(
                        self.provider_name,
                        f"HTTP {response.status_code}: {body[:300].decode(errors='replace')}",
                        status_code=response.status_code,
                        # Groq's free tier returns 429 aggressively — always
                        # treat it as retryable so the breaker/failover kicks in
                        # rather than surfacing the error to the user.
                        retryable=response.status_code in (429, 500, 502, 503, 504),
                    )

                async for raw_line in response.aiter_lines():
                    if not raw_line or not raw_line.startswith("data:"):
                        continue

                    payload_str = raw_line[len("data:"):].strip()
                    if payload_str == "[DONE]":
                        return

                    try:
                        event = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue

                    choices = event.get("choices") or []
                    if not choices:
                        continue

                    delta_obj = choices[0].get("delta") or {}
                    delta_text = delta_obj.get("content") or ""
                    finish_reason = choices[0].get("finish_reason")

                    if not delta_text and finish_reason is None:
                        continue

                    yield StreamChunk(
                        delta=delta_text,
                        is_first_token=not first_token_sent and bool(delta_text),
                        finish_reason=finish_reason,
                        elapsed_ms=self._now_ms(start),
                    )
                    first_token_sent = first_token_sent or bool(delta_text)

        except httpx.TimeoutException as exc:
            raise ProviderError(self.provider_name, f"timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.provider_name, f"transport error: {exc}", retryable=True) from exc
