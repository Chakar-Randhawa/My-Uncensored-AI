"""
The racing controller.

`RacingEngine.race()` starts every eligible provider's stream concurrently
inside an `asyncio.TaskGroup`. The first task to produce a real token wins
an atomic compare-and-set on `winner`; every other in-flight task is then
explicitly `.cancel()`'d, which propagates `asyncio.CancelledError` up
through the adapter's `async with self.client.stream(...)` block and closes
that provider's httpx connection immediately — this is the "execution-abort
hook" that stops burning free-tier rate limit on providers that already
lost.

The winner's chunks are relayed to the caller through an `asyncio.Queue` so
`race()` can be consumed as a plain `AsyncIterator[StreamChunk]` by the SSE
layer, while `self.last_result` accumulates full per-provider telemetry
(status/ttft/error) for the async Supabase log write once the generator is
exhausted.
"""

from __future__ import annotations

import asyncio
import logging

from collections.abc import AsyncIterator

from app.adapters.base import BaseModelAdapter, ProviderError, StreamChunk
from app.core.circuit_breaker import CircuitBreaker
from app.models.schemas import ChatMessage, ProviderRaceResult, ProviderStatus

logger = logging.getLogger("ai_router.racing_engine")

# Sentinel pushed onto the relay queue to signal "the winner's stream is over".
_STREAM_DONE = object()


class NoProvidersAvailableError(RuntimeError):
    """Every candidate provider is either circuit-broken open or failed before any token arrived."""


class RaceResult:
    __slots__ = ("winner", "results")

    def __init__(self) -> None:
        self.winner: str | None = None
        self.results: dict[str, ProviderRaceResult] = {}


class RacingEngine:
    def __init__(self, *, circuit_breaker: CircuitBreaker, race_timeout_seconds: float) -> None:
        self._breaker = circuit_breaker
        self._race_timeout_seconds = race_timeout_seconds
        self.last_result = RaceResult()

    async def race(
        self,
        adapters: list[BaseModelAdapter],
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[StreamChunk]:
        eligible = [a for a in adapters if await self._breaker.is_available(a.provider_name)]
        if not eligible:
            raise NoProvidersAvailableError(
                "All configured providers are circuit-broken open — try again shortly."
            )

        self.last_result = RaceResult()
        results = {
            a.provider_name: ProviderRaceResult(provider=a.provider_name, status=ProviderStatus.CANCELLED)
            for a in eligible
        }
        self.last_result.results = results

        relay: asyncio.Queue[StreamChunk | object] = asyncio.Queue()
        winner_lock = asyncio.Lock()
        winner_box: dict[str, str | None] = {"name": None}
        winner_declared = asyncio.Event()
        tasks: dict[str, asyncio.Task[None]] = {}

        async def run_candidate(adapter: BaseModelAdapter) -> None:
            name = adapter.provider_name
            is_this_the_winner = False
            try:
                async with adapter:
                    first_chunk_seen = False
                    async for chunk in adapter.stream(
                        messages, temperature=temperature, max_tokens=max_tokens
                    ):
                        if not first_chunk_seen:
                            first_chunk_seen = True
                            async with winner_lock:
                                if winner_box["name"] is None:
                                    winner_box["name"] = name
                            is_this_the_winner = winner_box["name"] == name
                            results[name].ttft_ms = chunk.elapsed_ms

                            if is_this_the_winner:
                                results[name].status = ProviderStatus.WON
                                winner_declared.set()
                            else:
                                # Lost the race on the very first token — bail
                                # out without relaying anything. The
                                # supervisor will cancel this task's
                                # connection momentarily; returning here also
                                # drops us out of the `async with adapter`
                                # block, closing the client proactively.
                                return

                        if not is_this_the_winner:
                            return
                        await relay.put(chunk)

                if is_this_the_winner:
                    await self._breaker.record_success(name)

            except ProviderError as exc:
                results[name].status = ProviderStatus.ERROR
                results[name].error = str(exc)
                if exc.retryable:
                    await self._breaker.record_failure(name)
                if is_this_the_winner:
                    # Winner died mid-stream (e.g. connection dropped after
                    # the first token) — surface a terminal chunk instead of
                    # hanging the SSE consumer forever.
                    await relay.put(
                        StreamChunk(delta="", finish_reason="provider_error", elapsed_ms=0)
                    )

            except asyncio.CancelledError:
                # Expected for every losing candidate once a winner is
                # declared — not an error condition.
                if results[name].status == ProviderStatus.CANCELLED:
                    pass
                raise

            finally:
                if is_this_the_winner:
                    await relay.put(_STREAM_DONE)

        async def supervisor() -> None:
            """Cancel every non-winning task the instant a winner is known."""
            await winner_declared.wait()
            winner_name = winner_box["name"]
            for provider_name, task in tasks.items():
                if provider_name != winner_name and not task.done():
                    task.cancel()

        async def run_race() -> None:
            async with asyncio.TaskGroup() as tg:
                for adapter in eligible:
                    tasks[adapter.provider_name] = tg.create_task(
                        run_candidate(adapter), name=f"race:{adapter.provider_name}"
                    )
                tg.create_task(supervisor(), name="race:supervisor")

        race_task = asyncio.ensure_future(run_race())

        try:
            async with asyncio.timeout(self._race_timeout_seconds):
                while True:
                    item = await relay.get()
                    if item is _STREAM_DONE:
                        break
                    assert isinstance(item, StreamChunk)
                    yield item
        except TimeoutError as exc:
            race_task.cancel()
            if winner_box["name"] is None:
                raise NoProvidersAvailableError(
                    f"No provider produced a token within {self._race_timeout_seconds}s."
                ) from exc
            # A winner existed but stalled mid-stream past the deadline —
            # let the caller's SSE layer close the connection gracefully.
            return
        finally:
            self.last_result.winner = winner_box["name"]
            if not race_task.done():
                await asyncio.gather(race_task, return_exceptions=True)
            else:
                exc = race_task.exception()
                if exc is not None:
                    logger.warning("racing engine task group raised after streaming: %r", exc)
