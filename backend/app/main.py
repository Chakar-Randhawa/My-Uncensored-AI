"""
AI Router Platform — FastAPI backend entrypoint.

POST /v1/chat/stream is the only endpoint that matters: it fans a chat
request out to every eligible provider concurrently, streams the winner's
tokens back over SSE as they're produced, and — only after the stream has
fully reached the client — fires an async, non-blocking write of the
complete transaction to Supabase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from app.adapters.base import BaseModelAdapter, ProviderConfig
from app.adapters.groq import GroqAdapter
from app.adapters.openrouter import OpenRouterAdapter
from app.auth import AuthenticatedUser, get_current_user
from app.config import Settings, get_settings
from app.core.circuit_breaker import CircuitBreaker
from app.core.racing_engine import NoProvidersAvailableError, RacingEngine
from app.models.schemas import (
    ChatCompletionRequest,
    ProviderRaceResult,
    StreamedAssistantMessage,
)
from app.services.supabase_logger import SupabaseLogger

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai_router.main")

# Model choice per provider — free-tier-friendly defaults. Override via
# `provider_priority` in the request, or move these to Settings if you want
# them environment-configurable without a code change.
_PROVIDER_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "meta-llama/llama-3.1-8b-instruct:free",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.circuit_breaker = CircuitBreaker(
        failure_threshold=settings.circuit_breaker_failure_threshold,
        reset_seconds=settings.circuit_breaker_reset_seconds,
    )
    app.state.supabase_logger = SupabaseLogger(settings)
    logger.info("AI Router Platform started (env=%s)", settings.environment)
    yield


app = FastAPI(
    title="AI Router Platform",
    version="1.0.0",
    lifespan=lifespan,
)


def _build_adapters(settings: Settings, priority: list[str] | None) -> list[BaseModelAdapter]:
    """Construct fresh (un-entered) adapter instances for one request.

    Adapters own an httpx.AsyncClient with connection-level state, so we
    build new instances per request rather than sharing singletons across
    concurrent users.
    """
    registry: dict[str, BaseModelAdapter] = {
        "groq": GroqAdapter(
            ProviderConfig(
                api_key=settings.groq_api_key,
                base_url="https://api.groq.com",
                model=_PROVIDER_MODELS["groq"],
                connect_timeout_seconds=settings.provider_connect_timeout_seconds,
                read_timeout_seconds=settings.provider_read_timeout_seconds,
            )
        ),
        "openrouter": OpenRouterAdapter(
            ProviderConfig(
                api_key=settings.openrouter_api_key,
                base_url="https://openrouter.ai",
                model=_PROVIDER_MODELS["openrouter"],
                connect_timeout_seconds=settings.provider_connect_timeout_seconds,
                read_timeout_seconds=settings.provider_read_timeout_seconds,
                extra_headers={"app_title": "AI Router Platform"},
            )
        ),
    }

    order = priority or list(registry.keys())
    return [registry[name] for name in order if name in registry]


@app.get("/health")
async def health(request: Request) -> dict:
    breaker: CircuitBreaker = request.app.state.circuit_breaker
    return {"status": "ok", "circuit_breakers": breaker.snapshot()}


@app.post("/v1/chat/stream")
async def chat_stream(
    payload: ChatCompletionRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> EventSourceResponse:
    settings: Settings = request.app.state.settings
    breaker: CircuitBreaker = request.app.state.circuit_breaker
    supabase_logger: SupabaseLogger = request.app.state.supabase_logger

    adapters = _build_adapters(settings, payload.provider_priority)
    if not adapters:
        raise HTTPException(status_code=400, detail="No valid providers in provider_priority.")

    conversation_id = payload.conversation_id or uuid.uuid4()
    engine = RacingEngine(
        circuit_breaker=breaker, race_timeout_seconds=settings.race_timeout_seconds
    )

    messages = list(payload.messages)
    if payload.system_prompt:
        # User-supplied, per-conversation system prompt only. The platform
        # injects nothing of its own.
        from app.models.schemas import ChatMessage, ChatRole  # local import avoids a cycle at module load

        messages = [ChatMessage(role=ChatRole.SYSTEM, content=payload.system_prompt), *messages]

    async def event_generator() -> AsyncIterator[dict]:
        start = time.perf_counter()
        chunks: list[str] = []
        token_count = 0
        stream_error: str | None = None

        try:
            async for chunk in engine.race(
                adapters,
                messages,
                temperature=payload.temperature,
                max_tokens=payload.max_tokens,
            ):
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

                if chunk.finish_reason and chunk.finish_reason != "provider_error":
                    yield {"event": "done", "data": json.dumps({"finish_reason": chunk.finish_reason})}

        except NoProvidersAvailableError as exc:
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}
            return
        except asyncio.CancelledError:
            # Client disconnected mid-stream — nothing left to send.
            raise
        finally:
            total_duration_ms = int((time.perf_counter() - start) * 1000)
            winner = engine.last_result.winner
            provider_meta: list[ProviderRaceResult] = list(engine.last_result.results.values())
            full_content = "".join(chunks)

            if winner and full_content:
                winning_ttft = provider_meta_lookup_ttft(provider_meta, winner)
                asyncio.create_task(
                    _persist_transaction(
                        supabase_logger=supabase_logger,
                        conversation_id=conversation_id,
                        user_id=user.id,
                        user_message=payload.messages[-1].content,
                        assistant_message=StreamedAssistantMessage(
                            conversation_id=conversation_id,
                            user_id=user.id,
                            content=full_content,
                            winning_provider=winner,
                            winning_model=_PROVIDER_MODELS.get(winner, "unknown"),
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


def provider_meta_lookup_ttft(results: list[ProviderRaceResult], provider: str) -> int | None:
    for result in results:
        if result.provider == provider:
            return result.ttft_ms
    return None


async def _persist_transaction(
    *,
    supabase_logger: SupabaseLogger,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    user_message: str,
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


def _configure_cors(application: FastAPI, settings: Settings) -> None:
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )


_configure_cors(app, get_settings())
