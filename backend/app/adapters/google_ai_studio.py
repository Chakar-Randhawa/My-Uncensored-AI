"""
Google AI Studio (Gemini) adapter.

Materially different wire format from the other five providers, so this
does NOT extend `OpenAICompatibleAdapter`:

  * Auth is an `x-goog-api-key` header, not `Authorization: Bearer`.
  * Messages are `contents: [{role, parts: [...]}]`, roles are
    `user`/`model` (not `assistant`), and a system prompt is a separate
    top-level `systemInstruction` field rather than a message in the list.
  * Streaming uses `:streamGenerateContent?alt=sse` — SSE-framed, but each
    frame is a full `GenerateContentResponse` object, not an OpenAI-style
    delta, and there is no terminal `data: [DONE]` sentinel; the stream
    simply closes when generation finishes.
  * Images are inline base64 (`inlineData`), not `image_url` — so only
    `data:image/...` refs are usable here; a plain `https://` image URL is
    silently dropped for this provider specifically (Gemini requires its
    separate Files API for remote fetch, out of scope for a free-tier
    router).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from app.adapters.base import BaseModelAdapter, ProviderError, StreamChunk
from app.models.schemas import ChatMessage, ChatRole


class GoogleAIStudioAdapter(BaseModelAdapter):
    provider_name = "google_ai_studio"

    def _build_headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self.config.api_key,
            "Content-Type": "application/json",
        }

    def _build_parts(self, message: ChatMessage) -> list[dict]:
        parts: list[dict] = [{"text": message.content}]
        if not self.config.supports_vision:
            return parts

        for image_ref in message.images:
            if not image_ref.startswith("data:image/"):
                continue  # remote https URLs unsupported without the Files API
            header, _, b64_data = image_ref.partition(",")
            mime_type = header.removeprefix("data:").split(";")[0] or "image/png"
            parts.append({"inlineData": {"mimeType": mime_type, "data": b64_data}})
        return parts

    def _build_payload(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
    ) -> dict:
        system_parts = [m.content for m in messages if m.role == ChatRole.SYSTEM]
        contents = [
            {
                "role": "model" if m.role == ChatRole.ASSISTANT else "user",
                "parts": self._build_parts(m),
            }
            for m in messages
            if m.role != ChatRole.SYSTEM
        ]

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        return payload

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
        path = f"/v1beta/models/{self.config.model}:streamGenerateContent?alt=sse"

        try:
            async with self.client.stream("POST", path, json=payload) as response:
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
                    if not payload_str:
                        continue

                    try:
                        event = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue

                    candidates = event.get("candidates") or []
                    if not candidates:
                        continue

                    candidate = candidates[0]
                    delta_text = "".join(
                        part.get("text", "") for part in candidate.get("content", {}).get("parts", [])
                    )
                    # Gemini's finishReason values ("STOP", "MAX_TOKENS", ...)
                    # don't map to OpenAI's, but the racing engine only checks
                    # *presence* to know the stream ended — normalize to a
                    # generic marker.
                    finish_reason = "stop" if candidate.get("finishReason") else None

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
