"""
OpenRouter adapter.

OpenRouter's endpoint is OpenAI-compatible (handled by the shared base),
but it also sends SSE *comment* lines (`: OPENROUTER PROCESSING`) as
keep-alive pings while it queues the request upstream — the shared base's
parser already skips any line starting with `:`, so no override is needed
there. This subclass only adds OpenRouter's attribution headers, which
affect free-tier routing priority.
"""

from __future__ import annotations

from app.adapters.openai_compatible import OpenAICompatibleAdapter


class OpenRouterAdapter(OpenAICompatibleAdapter):
    provider_name = "openrouter"
    chat_path = "/api/v1/chat/completions"

    def _build_headers(self) -> dict[str, str]:
        return {
            **super()._build_headers(),
            "HTTP-Referer": self.config.extra_headers.get("referer", "https://localhost"),
            "X-Title": self.config.extra_headers.get("app_title", "AI Router Platform"),
        }
