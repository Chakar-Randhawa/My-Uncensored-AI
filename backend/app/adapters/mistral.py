"""
Mistral adapter.

Mistral's "La Plateforme" free tier (rate-limited but no cost) serves
their own model family — a genuinely different model lineage from the
Llama-derivatives the other providers mostly serve, useful both as a
racing entrant and as a fallback with different failure characteristics.
"""

from __future__ import annotations

from app.adapters.openai_compatible import OpenAICompatibleAdapter


class MistralAdapter(OpenAICompatibleAdapter):
    provider_name = "mistral"
    chat_path = "/v1/chat/completions"
