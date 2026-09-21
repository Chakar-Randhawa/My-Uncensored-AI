"""
Cerebras adapter.

Cerebras Cloud's free tier serves Llama models on their Wafer-Scale Engine
hardware — throughput competitive with or exceeding Groq's on supported
models, making it a genuine third contender in the race rather than a
pure fallback.
"""

from __future__ import annotations

from app.adapters.openai_compatible import OpenAICompatibleAdapter


class CerebrasAdapter(OpenAICompatibleAdapter):
    provider_name = "cerebras"
    chat_path = "/v1/chat/completions"
