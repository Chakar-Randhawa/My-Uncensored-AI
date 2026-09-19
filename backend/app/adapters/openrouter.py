"""
OpenRouter adapter.

OpenRouter exposes an OpenAI-compatible `/api/v1/chat/completions` endpoint
with `stream: true`, emitting Server-Sent Events as `data: {...}\n\n` lines
terminated by a literal `data: [DONE]`. OpenRouter also periodically sends
SSE *comment* lines (`: OPENROUTER PROCESSING`) as keep-alive pings while it
queues the request upstream — these are not valid JSON and must be skipped
rather than treated as a parse error.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from app.adapters.base import BaseModelAdapter, ProviderConfig, ProviderError, StreamChunk
from app.models.schemas import ChatMessage


class OpenRouterAdapter(BaseModelAdapter):
    provider_name = "openrouter"

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            # OpenRouter uses these for its public leaderboard attribution —
            # harmless to omit, but good practice / sometimes required for
            # free-tier routing priority.
            "HTTP-Referer": self.config.extra_headers.get("referer", "https://localhost"),
            "X-Title": self.config.extra_headers.get("app_title", "AI Router Platform"),
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
                "POST", "/api/v1/chat/completions", json=payload
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise ProviderError(
                        self.provider_name,
                        f"HTTP {response.status_code}: {body[:300].decode(errors='replace')}",
                        status_code=response.status_code,
                        retryable=response.status_code in (429, 500, 502, 503, 504),
                    )

                async for raw_line in response.aiter_lines():
                    if not raw_line:
                        continue
                    # Keep-alive comment pings — not data, skip.
                    if raw_line.startswith(":"):
                        continue
                    if not raw_line.startswith("data:"):
                        continue

                    payload_str = raw_line[len("data:"):].strip()
                    if payload_str == "[DONE]":
                        return

                    try:
                        event = json.loads(payload_str)
                    except json.JSONDecodeError:
                        # Malformed/partial frame — skip rather than kill the race.
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
