"""
Non-blocking Supabase persistence.

We talk to Supabase's auto-generated PostgREST API directly over httpx
rather than pulling in the full `supabase-py` SDK: it's one dependency
lighter, and — more importantly — it gives us a plain `AsyncClient` we
fully control, so a slow/failed insert can never block or crash the
request path that already streamed a complete answer to the user.

Every public function here is designed to be wrapped in
`asyncio.create_task(...)` and forgotten: failures are logged, never
raised, because by the time this module runs the user has already
received their tokens.
"""

from __future__ import annotations

import logging
from uuid import UUID

import httpx

from app.config import Settings
from app.models.schemas import StreamedAssistantMessage

logger = logging.getLogger("ai_router.supabase_logger")


class SupabaseLogger:
    def __init__(self, settings: Settings) -> None:
        self._base_url = str(settings.supabase_url).rstrip("/")
        self._headers = {
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "Content-Type": "application/json",
            # service_role bypasses RLS; Prefer=minimal skips echoing the
            # inserted row back, saving a little bandwidth/latency.
            "Prefer": "return=minimal",
        }

    async def ensure_conversation(
        self, *, conversation_id: UUID, user_id: UUID, first_user_message: str
    ) -> None:
        """Upsert a conversation row so a fresh chat has a parent before its first message lands."""
        title = (first_user_message[:80] + "…") if len(first_user_message) > 80 else first_user_message
        body = {
            "id": str(conversation_id),
            "user_id": str(user_id),
            "title": title or "New conversation",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(
                    f"{self._base_url}/rest/v1/conversations",
                    headers={**self._headers, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                    json=body,
                )
                response.raise_for_status()
            except httpx.HTTPError:
                logger.exception("Failed to upsert conversation %s", conversation_id)

    async def log_user_message(
        self, *, conversation_id: UUID, user_id: UUID, content: str
    ) -> None:
        body = {
            "conversation_id": str(conversation_id),
            "user_id": str(user_id),
            "role": "user",
            "content": content,
        }
        await self._insert("messages", body)

    async def log_assistant_message(self, message: StreamedAssistantMessage) -> None:
        body = {
            "conversation_id": str(message.conversation_id),
            "user_id": str(message.user_id),
            "role": "assistant",
            "content": message.content,
            "winning_provider": message.winning_provider,
            "winning_model": message.winning_model,
            "latency_ms": message.latency_ms,
            "total_duration_ms": message.total_duration_ms,
            "token_count": message.token_count,
            "provider_meta": [r.model_dump(mode="json") for r in message.provider_meta],
        }
        await self._insert("messages", body)

    async def _insert(self, table: str, body: dict) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(
                    f"{self._base_url}/rest/v1/{table}",
                    headers=self._headers,
                    json=body,
                )
                response.raise_for_status()
            except httpx.HTTPError:
                # Deliberately swallowed: logging failures must never
                # surface to the user or affect the chat response they
                # already received.
                logger.exception("Failed to write to Supabase table '%s'", table)
