"""
Semantic response cache.

Deliberately NOT using an external embeddings API (OpenAI/Cohere/etc.) —
that would mean a 7th network dependency and a 7th free-tier quota to
exhaust. Instead we embed locally with `fastembed`
(BAAI/bge-small-en-v1.5, quantized ONNX, ~130MB on disk, CPU-only, no
PyTorch) which comfortably fits Render's free-tier container.

The model loads once per process (module-level singleton) — first request
after a cold start pays the load cost (~1-2s), every request after that
is fast (~10-30ms to embed a short query on CPU).

Cache scope is intentionally narrow: `main.py` only *consults* the cache
for single-turn requests (no prior conversation history), so a cache hit
can never return a stale answer to a follow-up question that depended on
context the cache entry didn't have.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings

logger = logging.getLogger("ai_router.semantic_cache")

_EMBEDDING_DIM = 384
_MODEL_NAME = "BAAI/bge-small-en-v1.5"

_embedding_model = None  # lazy singleton, see _get_model()


def _get_model():
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding  # imported lazily: skip the load

        # entirely for requests where semantic_cache_enabled is False, and
        # keep it out of module import time for faster cold starts.
        logger.info("Loading semantic cache embedding model '%s' (first use)…", _MODEL_NAME)
        _embedding_model = TextEmbedding(model_name=_MODEL_NAME)
    return _embedding_model


class SemanticCache:
    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.semantic_cache_enabled
        self._threshold = settings.semantic_cache_similarity_threshold
        self._base_url = str(settings.supabase_url).rstrip("/")
        self._headers = {
            # See the identical comment in supabase_logger.py — apikey
            # only, never also as an Authorization bearer token.
            "apikey": settings.supabase_secret_key,
            "Content-Type": "application/json",
        }

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _embed(self, text: str) -> list[float]:
        model = _get_model()
        # fastembed's .embed() returns a generator of numpy arrays.
        (vector,) = model.embed([text])
        return vector.tolist()

    async def find_similar(self, query: str) -> dict | None:
        """Returns {"response_text", "provider", "similarity"} on a hit, else None."""
        if not self._enabled:
            return None

        try:
            embedding = self._embed(query)
        except Exception:
            logger.exception("Embedding generation failed — skipping cache lookup.")
            return None

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    f"{self._base_url}/rest/v1/rpc/match_cached_response",
                    headers=self._headers,
                    json={
                        "p_query_embedding": embedding,
                        "match_threshold": self._threshold,
                        "match_count": 1,
                    },
                )
                response.raise_for_status()
                rows = response.json()
        except httpx.HTTPError:
            logger.exception("Semantic cache lookup failed — falling through to a live race.")
            return None

        if not rows:
            return None

        match = rows[0]
        logger.info(
            "Semantic cache hit (similarity=%.3f, provider=%s)",
            match["similarity"], match["provider"],
        )
        return match

    async def store(self, *, query: str, response_text: str, provider: str) -> None:
        if not self._enabled or not response_text.strip():
            return

        try:
            embedding = self._embed(query)
        except Exception:
            logger.exception("Embedding generation failed — skipping cache write.")
            return

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    f"{self._base_url}/rest/v1/response_cache",
                    headers={**self._headers, "Prefer": "return=minimal"},
                    json={
                        "query_text": query,
                        "query_embedding": embedding,
                        "response_text": response_text,
                        "provider": provider,
                    },
                )
                resp.raise_for_status()
        except httpx.HTTPError:
            logger.exception("Failed to write semantic cache entry.")
