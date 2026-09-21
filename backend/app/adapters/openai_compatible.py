"""
Shared implementation for every provider that speaks the OpenAI
`/chat/completions` wire format with `stream: true` SSE (`data: {...}`
lines, terminated by `data: [DONE]`). That's four of our six providers —
Groq, OpenRouter, Cerebras, Mistral — so this base class owns the payload
shape and the SSE parser once; each subclass only supplies its base URL,
auth header, model name, and whether that model accepts image input.

Google AI Studio (Gemini) and Cloudflare Workers AI use materially
different wire formats and are NOT built on this base — see
`google_ai_studio.py` and `cloudflare_workers_ai.py`.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from app.adapters.base import BaseModelAdapter, ProviderError, StreamChunk
from app.models.schemas import ChatMessage


class OpenAICompatibleAdapter(BaseModelAdapter):
    #: POST path for chat completions, relative to `config.base_url`.
    chat_path: str = "/v1/chat/completions"

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _build_message_content(self, message: ChatMessage) -> str | list[dict]:
        if not message.images or not self.config.supports_vision:
            return message.content

        parts: list[dict] = [{"type": "text", "text": message.content}]
        for image_ref in message.images:
            parts.append({"type": "image_url", "image_url": {"url": image_ref}})
        return parts

    def _build_payload(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
    ) -> dict:
        return {
            "model": self.config.model,
            "messages": [
                {"role": m.role.value, "content": self._build_message_content(m)}
                for m in messages
            ],
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
            async with self.client.stream("POST", self.chat_path, json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise ProviderError(
                        self.provider_name,
                        f"HTTP {response.status_code}: {body[:300].decode(errors='replace')}",
                        status_code=response.status_code,
                        retryable=response.status_code in (429, 500, 502, 503, 504),
                    )

                async for raw_line in response.aiter_lines():
                    if not raw_line or raw_line.startswith(":"):
                        continue
                    if not raw_line.startswith("data:"):
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
