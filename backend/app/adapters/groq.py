"""
Groq adapter.

On LPU hardware Groq is typically the fastest entrant in the race by a
wide margin — exactly why it's worth racing rather than hardcoding as
"the" provider: its free tier has the tightest rate limits of the pool,
so the circuit breaker needs to open on it aggressively and let the race
fall through to the others.
"""

from __future__ import annotations

from app.adapters.openai_compatible import OpenAICompatibleAdapter


class GroqAdapter(OpenAICompatibleAdapter):
    provider_name = "groq"
    chat_path = "/openai/v1/chat/completions"
