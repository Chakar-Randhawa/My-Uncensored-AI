"""
AI Router Platform — FastAPI backend entrypoint.

POST /v1/chat/stream is still the core endpoint: race every eligible
provider concurrently, stream the winner's tokens back over SSE, retry
with a different provider if the winner dies mid-stream, and — only after
the client has everything — fire an async, non-blocking Supabase write.

New in this revision:
  * Full conversation history is accepted in `messages` (the frontend now
    sends it; nothing changed backend-side for this — it was always
    supported, the gap was the frontend not populating it).
  * `request.is_disconnected()` is polled during streaming so hitting
    "Stop" in the UI actually cancels the upstream provider connection,
    not just the browser's read of the SSE response.
  * Mid-stream provider failure triggers an automatic retry with a
    different provider via `engine.race_with_failover`.
  * A semantic cache is consulted for single-turn requests before racing
    any provider at all.
  * Six provider adapters instead of two, with vision-aware routing.
  * `/v1/admin/stats` for the admin dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from app.adapters.base import BaseModelAdapter, ProviderConfig
from app.adapters.cerebras import CerebrasAdapter
from app.adapters.cloudflare_workers_ai import CloudflareWorkersAIAdapter
from app.adapters.google_ai_studio import GoogleAIStudioAdapter
from app.adapters.groq import GroqAdapter
from app.adapters.mistral import MistralAdapter
from app.adapters.openrouter import OpenRouterAdapter
from app.auth import AuthenticatedUser, get_current_user
from app.config import Settings, get_settings
from app.core.circuit_breaker import CircuitBreakerProtocol, build_circuit_breaker
from app.core.racing_engine import NoProvidersAvailableError, RacingEngine
from app.logging_config import configure_logging
from app.models.schemas import (
    ChatCompletionRequest,
    ChatMessage,
    ChatRole,
    ProviderRaceResult,
    StreamedAssistantMessage,
)
from app.services.semantic_cache import SemanticCache
from app.services.supabase_logger import SupabaseLogger

logger = logging.getLogger("ai_router.main")

# Model per provider, split into text/vision variants. `vision: None` means
# that provider has no free-tier vision model — it's simply excluded from
# the race whenever the request includes images, rather than silently
# answering a question about an image it never saw.
_PROVIDER_MODELS: dict[str, dict[str, str | None]] = {
    "groq": {"text": "llama-3.3-70b-versatile", "vision": "llama-3.2-11b-vision-preview"},
    "openrouter": {
        "text": "meta-llama/llama-3.1-8b-instruct:free",
        "vision": "meta-llama/llama-3.2-11b-vision-instruct:free",
    },
    "cerebras": {"text": "llama-3.3-70b", "vision": None},
    "mistral": {"text": "mistral-small-latest", "vision": None},
    "google_ai_studio": {"text": "gemini-1.5-flash", "vision": "gemini-1.5-flash"},
    "cloudflare_workers_ai": {"text": "@cf/meta/llama-3.1-8b-instruct", "vision": None},
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(environment=settings.environment)

    if settings.sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment, traces_sample_rate=0.1)
        logger.info("Sentry initialized.")

    app.state.settings = settings
    app.state.circuit_breaker = build_circuit_breaker(
        failure_threshold=settings.circuit_breaker_failure_threshold,
        reset_seconds=settings.circuit_breaker_reset_seconds,
        upstash_rest_url=settings.upstash_redis_rest_url,
        upstash_rest_token=settings.upstash_redis_rest_token,
    )
    app.state.supabase_logger = SupabaseLogger(settings)
    app.state.semantic_cache = SemanticCache(settings)

    breaker_kind = "redis" if settings.upstash_redis_rest_url else "in-memory"
    logger.info(
        "AI Router Platform started",
        extra={"environment": settings.environment, "circuit_breaker": breaker_kind},
    )
    yield


app = FastAPI(title="AI Router Platform", version="2.0.0", lifespan=lifespan)


def _build_adapters(
    settings: Settings, priority: list[str] | None, *, needs_vision: bool
) -> list[BaseModelAdapter]:
    """Construct fresh (un-entered) adapter instances for one request.

    Adapters own an httpx.AsyncClient with connection-level state, so we
    build new instances per request rather than sharing singletons across
    concurrent users. When `needs_vision` is true, providers with no free
    vision model are left out entirely rather than included and silently
    ignoring the image.
    """
    candidates: dict[str, BaseModelAdapter | None] = {}

    def model_for(provider: str) -> str | None:
        variants = _PROVIDER_MODELS[provider]
        return variants["vision"] if needs_vision else variants["text"]

    groq_model = model_for("groq")
    if settings.groq_api_key and groq_model:
        candidates["groq"] = GroqAdapter(
            ProviderConfig(
                api_key=settings.groq_api_key,
                base_url="https://api.groq.com",
                model=groq_model,
                connect_timeout_seconds=settings.provider_connect_timeout_seconds,
                read_timeout_seconds=settings.provider_read_timeout_seconds,
                supports_vision=needs_vision,
            )
        )

    openrouter_model = model_for("openrouter")
    if settings.openrouter_api_key and openrouter_model:
        candidates["openrouter"] = OpenRouterAdapter(
            ProviderConfig(
                api_key=settings.openrouter_api_key,
                base_url="https://openrouter.ai",
                model=openrouter_model,
                connect_timeout_seconds=settings.provider_connect_timeout_seconds,
                read_timeout_seconds=settings.provider_read_timeout_seconds,
                extra_headers={"app_title": "AI Router Platform"},
                supports_vision=needs_vision,
            )
        )

    cerebras_model = model_for("cerebras")
    if settings.cerebras_api_key and cerebras_model:
        candidates["cerebras"] = CerebrasAdapter(
            ProviderConfig(
                api_key=settings.cerebras_api_key,
                base_url="https://api.cerebras.ai",
                model=cerebras_model,
                connect_timeout_seconds=settings.provider_connect_timeout_seconds,
                read_timeout_seconds=settings.provider_read_timeout_seconds,
            )
        )

    mistral_model = model_for("mistral")
    if settings.mistral_api_key and mistral_model:
        candidates["mistral"] = MistralAdapter(
            ProviderConfig(
                api_key=settings.mistral_api_key,
                base_url="https://api.mistral.ai",
                model=mistral_model,
                connect_timeout_seconds=settings.provider_connect_timeout_seconds,
                read_timeout_seconds=settings.provider_read_timeout_seconds,
            )
        )

    google_model = model_for("google_ai_studio")
    if settings.google_ai_studio_api_key and google_model:
        candidates["google_ai_studio"] = GoogleAIStudioAdapter(
            ProviderConfig(
                api_key=settings.google_ai_studio_api_key,
                base_url="https://generativelanguage.googleapis.com",
                model=google_model,
                connect_timeout_seconds=settings.provider_connect_timeout_seconds,
                read_timeout_seconds=settings.provider_read_timeout_seconds,
                supports_vision=needs_vision,
            )
        )

    cf_model = model_for("cloudflare_workers_ai")
    if settings.cloudflare_account_id and settings.cloudflare_api_token and cf_model:
        candidates["cloudflare_workers_ai"] = CloudflareWorkersAIAdapter(
            ProviderConfig(
                api_key=settings.cloudflare_api_token,
                base_url="https://api.cloudflare.com",
                model=cf_model,
                connect_timeout_seconds=settings.provider_connect_timeout_seconds,
                read_timeout_seconds=settings.provider_read_timeout_seconds,
                extra_headers={"account_id": settings.cloudflare_account_id},
            )
        )

    order = priority or list(candidates.keys())
    return [candidates[name] for name in order if candidates.get(name) is not None]


@app.get("/health")
async def health(request: Request) -> dict:
    breaker: CircuitBreakerProtocol = request.app.state.circuit_breaker
    return {"status": "ok", "circuit_breakers": await breaker.snapshot()}


@app.get("/v1/admin/stats")
async def admin_stats(
    request: Request, user: AuthenticatedUser = Depends(get_current_user)
) -> dict:
    """
    Aggregate provider win-rate / latency stats for the admin dashboard.
    Any authenticated user can view this in the current build (there's no
    separate admin role) — the data itself (provider performance, not
    message content) isn't sensitive, but tighten this with a role check
    before shipping to real users if that matters for your deployment.
    """
    settings: Settings = request.app.state.settings

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{str(settings.supabase_url).rstrip('/')}/rest/v1/messages",
            headers={
                # apikey only — see the comment in supabase_logger.py for why.
                "apikey": settings.supabase_secret_key,
            },
            params={
                "select": "winning_provider,latency_ms,total_duration_ms,token_count,created_at",
                "role": "eq.assistant",
                "order": "created_at.desc",
                "limit": "500",
            },
        )
        response.raise_for_status()
        rows = response.json()

    breaker: CircuitBreakerProtocol = request.app.state.circuit_breaker
    per_provider: dict[str, dict[str, float | int]] = {}
    for row in rows:
        provider = row.get("winning_provider")
        if not provider:
            continue
        bucket = per_provider.setdefault(
            provider, {"wins": 0, "total_latency_ms": 0, "total_tokens": 0}
        )
        bucket["wins"] += 1
        bucket["total_latency_ms"] += row.get("latency_ms") or 0
        bucket["total_tokens"] += row.get("token_count") or 0

    summary = {
        provider: {
            "wins": stats["wins"],
            "avg_latency_ms": round(stats["total_latency_ms"] / stats["wins"]) if stats["wins"] else 0,
            "total_tokens": stats["total_tokens"],
        }
        for provider, stats in per_provider.items()
    }

    return {
        "sample_size": len(rows),
        "provider_stats": summary,
        "circuit_breakers": await breaker.snapshot(),
    }


@app.post("/v1/chat/stream")
async def chat_stream(
    payload: ChatCompletionRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> EventSourceResponse:
    settings: Settings = request.app.state.settings
    breaker: CircuitBreakerProtocol = request.app.state.circuit_breaker
    supabase_logger: SupabaseLogger = request.app.state.supabase_logger
    semantic_cache: SemanticCache = request.app.state.semantic_cache

    needs_vision = any(m.images for m in payload.messages)
    adapters = _build_adapters(settings, payload.provider_priority, needs_vision=needs_vision)
    if not adapters:
        detail = (
            "No provider with vision support is configured."
            if needs_vision
            else "No providers configured or provider_priority matched nothing."
        )
        raise HTTPException(status_code=400, detail=detail)

    conversation_id = payload.conversation_id or uuid.uuid4()
    engine = RacingEngine(
        circuit_breaker=breaker, race_timeout_seconds=settings.race_timeout_seconds
    )

    messages = list(payload.messages)
    if payload.system_prompt:
        messages = [ChatMessage(role=ChatRole.SYSTEM, content=payload.system_prompt), *messages]

    # Semantic cache only applies to genuinely single-turn requests: no
    # prior conversation history means the cached answer can't be missing
    # context a follow-up question would need.
    is_single_turn = len(payload.messages) == 1 and not needs_vision
    latest_user_text = payload.messages[-1].content

    async def event_generator() -> AsyncIterator[dict]:
        start = time.perf_counter()

        if payload.use_cache and is_single_turn:
            cached = await semantic_cache.find_similar(latest_user_text)
            if cached:
                yield {
                    "event": "token",
                    "data": json.dumps(
                        {
                            "delta": cached["response_text"],
                            "conversation_id": str(conversation_id),
                            "cached": True,
                        }
                    ),
                }
                yield {
                    "event": "done",
                    "data": json.dumps({"finish_reason": "cache_hit", "provider": cached["provider"]}),
                }
                return

        chunks: list[str] = []
        token_count = 0
        stream_error: str | None = None
        race_gen = engine.race_with_failover(
            adapters, messages, temperature=payload.temperature, max_tokens=payload.max_tokens
        )

        try:
            while True:
                # Poll for client disconnect roughly every few tokens rather
                # than on every single one — `is_disconnected()` isn't free,
                # and token throughput matters more than sub-100ms
                # cancellation latency.
                if token_count % 4 == 0 and await request.is_disconnected():
                    await race_gen.aclose()
                    logger.info("Client disconnected mid-stream — upstream connection cancelled.")
                    return

                try:
                    chunk = await race_gen.__anext__()
                except StopAsyncIteration:
                    break

                if chunk.finish_reason == "failover_restart":
                    # A provider died mid-stream; the retry is starting from
                    # zero with a different provider. Clear whatever partial
                    # text the client already has instead of concatenating
                    # two different models' half-answers.
                    chunks.clear()
                    token_count = 0
                    yield {"event": "restart", "data": json.dumps({"reason": "provider_failover"})}
                    continue

                if chunk.finish_reason == "provider_error":
                    stream_error = "The selected provider disconnected mid-response."
                    break

                if chunk.delta:
                    chunks.append(chunk.delta)
                    token_count += 1
                    yield {
                        "event": "token",
                        "data": json.dumps(
                            {"delta": chunk.delta, "conversation_id": str(conversation_id)}
                        ),
                    }

                if chunk.finish_reason and chunk.finish_reason not in ("provider_error", "failover_restart"):
                    yield {"event": "done", "data": json.dumps({"finish_reason": chunk.finish_reason})}

        except NoProvidersAvailableError as exc:
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}
            return
        except asyncio.CancelledError:
            raise
        finally:
            total_duration_ms = int((time.perf_counter() - start) * 1000)
            winner = engine.last_result.winner
            provider_meta: list[ProviderRaceResult] = list(engine.last_result.results.values())
            full_content = "".join(chunks)

            if winner and full_content:
                winning_ttft = _lookup_ttft(provider_meta, winner)
                asyncio.create_task(
                    _persist_transaction(
                        supabase_logger=supabase_logger,
                        semantic_cache=semantic_cache,
                        conversation_id=conversation_id,
                        user_id=user.id,
                        user_message=latest_user_text,
                        should_cache=payload.use_cache and is_single_turn,
                        assistant_message=StreamedAssistantMessage(
                            conversation_id=conversation_id,
                            user_id=user.id,
                            content=full_content,
                            winning_provider=winner,
                            winning_model=str(_PROVIDER_MODELS.get(winner, {}).get(
                                "vision" if needs_vision else "text", "unknown"
                            )),
                            latency_ms=winning_ttft or 0,
                            total_duration_ms=total_duration_ms,
                            token_count=token_count,
                            provider_meta=provider_meta,
                        ),
                    )
                )

        if stream_error:
            yield {"event": "error", "data": json.dumps({"message": stream_error})}

    return EventSourceResponse(event_generator())


def _lookup_ttft(results: list[ProviderRaceResult], provider: str) -> int | None:
    for result in results:
        if result.provider == provider:
            return result.ttft_ms
    return None


async def _persist_transaction(
    *,
    supabase_logger: SupabaseLogger,
    semantic_cache: SemanticCache,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    user_message: str,
    should_cache: bool,
    assistant_message: StreamedAssistantMessage,
) -> None:
    """Background task body: everything here runs *after* the user already has their tokens."""
    await supabase_logger.ensure_conversation(
        conversation_id=conversation_id, user_id=user_id, first_user_message=user_message
    )
    await supabase_logger.log_user_message(
        conversation_id=conversation_id, user_id=user_id, content=user_message
    )
    await supabase_logger.log_assistant_message(assistant_message)

    if should_cache:
        await semantic_cache.store(
            query=user_message,
            response_text=assistant_message.content,
            provider=assistant_message.winning_provider,
        )


def _configure_cors(application: FastAPI, settings: Settings) -> None:
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )


_configure_cors(app, get_settings())
