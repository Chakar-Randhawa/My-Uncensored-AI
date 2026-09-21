"""
Cloudflare Workers AI adapter.

Cloudflare's inference API is account-scoped
(`/client/v4/accounts/{account_id}/ai/run/{model}`) rather than a single
global endpoint, and its streaming delta shape is
`data: {"response": "<token text>"}` — not OpenAI's `choices[0].delta`
— followed by a terminal `data: [DONE]`. Close enough to the OpenAI shape
that it's tempting to force it through the shared base, but the response
key difference and the account-scoped path make a dedicated adapter
clearer than a `chat_path` override plus a delta-shape branch.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from app.adapters.base import BaseModelAdapter, ProviderError, StreamChunk
from app.models.schemas import ChatMessage


class CloudflareWorkersAIAdapter(BaseModelAdapter):
    provider_name = "cloudflare_workers_ai"

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _run_path(self) -> str:
        account_id = self.config.extra_headers.get("account_id", "")
        return f"/client/v4/accounts/{account_id}/ai/run/{self.config.model}"

    def _build_payload(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
    ) -> dict:
        return {
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
            async with self.client.stream("POST", self._run_path(), json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise ProviderError(
                        self.provider_name,
                        f"HTTP {response.status_code}: {body[:300].decode(errors='replace')}",
                        status_code=response.status_code,
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

                    delta_text = event.get("response", "")
                    if not delta_text:
                        continue

                    yield StreamChunk(
                        delta=delta_text,
                        is_first_token=not first_token_sent,
                        finish_reason=None,
                        elapsed_ms=self._now_ms(start),
                    )
                    first_token_sent = True

        except httpx.TimeoutException as exc:
            raise ProviderError(self.provider_name, f"timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.provider_name, f"transport error: {exc}", retryable=True) from exc
