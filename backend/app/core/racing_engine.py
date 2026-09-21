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
from app.core.circuit_breaker import CircuitBreakerProtocol
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
    def __init__(self, *, circuit_breaker: CircuitBreakerProtocol, race_timeout_seconds: float) -> None:
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
        completed_normally = False

        try:
            async with asyncio.timeout(self._race_timeout_seconds):
                while True:
                    item = await relay.get()
                    if item is _STREAM_DONE:
                        completed_normally = True
                        break
                    assert isinstance(item, StreamChunk)
                    yield item
        except TimeoutError as exc:
            if winner_box["name"] is None:
                raise NoProvidersAvailableError(
                    f"No provider produced a token within {self._race_timeout_seconds}s."
                ) from exc
            # A winner existed but stalled mid-stream past the deadline —
            # let the caller's SSE layer close the connection gracefully.
            return
        finally:
            # IMPORTANT: only cancel race_task when we're leaving *early*
            # (a timeout above, or the caller closing this generator before
            # it finished naturally — see main.py's `race_gen.aclose()` on
            # client disconnect). On a normal, successful finish
            # (`completed_normally`), the TaskGroup's own child tasks
            # (winner + supervisor + already-cancelled losers) have either
            # already completed or are a single await away from it; calling
            # `race_task.cancel()` at that point injects a CancelledError
            # into `asyncio.TaskGroup.__aexit__` while it's finishing its
            # own bookkeeping, which re-raises as a CancelledError out of
            # this *entire* generator — turning a clean completion into a
            # crash. (This was a real bug: it broke every normal,
            # non-interrupted stream, caught by the test suite, not by
            # inspection.) Early-exit paths still need the explicit cancel
            # — that's the actual fix for "Stop only stopped the browser's
            # read, not the upstream provider connection."
            # only stopped the browser from *reading* the stream while the
            # backend kept burning free-tier rate limit against the
            # provider until it finished on its own.
            self.last_result.winner = winner_box["name"]
            if not completed_normally and not race_task.done():
                race_task.cancel()
            # Explicit try/except rather than
            # `asyncio.gather(race_task, return_exceptions=True)`: gather's
            # exception-capture has proven, in testing, not to reliably
            # swallow a CancelledError that originates from
            # `asyncio.TaskGroup.__aexit__` re-propagating after we cancel
            # its owning task — it can still escape and clobber whatever
            # exception this `finally` was already unwinding for (e.g. a
            # `NoProvidersAvailableError` raised just above). A plain
            # try/except around a direct `await` leaves no such gap.
            try:
                await race_task
            except asyncio.CancelledError:
                pass
            except Exception as cleanup_exc:  # pragma: no cover - defensive
                logger.warning("racing engine task group raised after streaming: %r", cleanup_exc)

    async def race_with_failover(
        self,
        adapters: list[BaseModelAdapter],
        messages: list[ChatMessage],
        *,
        temperature: float,
        max_tokens: int,
        max_attempts: int = 3,
    ) -> AsyncIterator[StreamChunk]:
        """
        Wraps `race()` with mid-stream failover: if the winning provider
        dies *after* it already sent tokens (connection drop, upstream 5xx
        mid-generation), we don't just error out to the user — we exclude
        that provider and start a fresh race among whoever's left.

        A restarted attempt can't "resume" a partial completion (none of
        these providers support continuing a half-finished generation from
        a different backend), so instead of stitching text together we
        signal the reset explicitly via a `finish_reason="failover_restart"`
        chunk. The SSE layer (see `main.py`) forwards this as a `restart`
        event so the frontend clears the partial answer before the retry's
        tokens start arriving — the alternative, silently concatenating two
        different models' half-answers, would produce a broken response.
        """
        remaining = list(adapters)
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            mid_stream_failed = False

            try:
                async for chunk in self.race(
                    remaining, messages, temperature=temperature, max_tokens=max_tokens
                ):
                    if chunk.finish_reason == "provider_error":
                        # Don't `break` here: breaking would abandon
                        # `self.race()`'s generator mid-flight, before its
                        # own `finally` block (which sets
                        # `self.last_result.winner`) has run — leaving
                        # `last_result.winner` at its stale/default value
                        # and silently breaking the "exclude the failed
                        # provider" logic below (it would filter against
                        # `None` and match nothing, so the same dead
                        # provider could be retried forever). Instead keep
                        # draining: the winner task has already stopped,
                        # so the next item is `_STREAM_DONE` and `race()`
                        # finishes naturally in one more step, running its
                        # `finally` block the normal way.
                        mid_stream_failed = True
                        continue
                    if mid_stream_failed:
                        continue  # defensive: nothing should follow a provider_error
                    yield chunk
            except NoProvidersAvailableError:
                raise

            if not mid_stream_failed:
                return  # clean finish (or caller-initiated stop) — done

            failed_provider = self.last_result.winner
            remaining = [a for a in remaining if a.provider_name != failed_provider]

            if not remaining:
                raise NoProvidersAvailableError(
                    "The winning provider disconnected mid-response and no other "
                    "provider was available to retry."
                )

            logger.info(
                "Provider '%s' failed mid-stream — retrying with %s (attempt %d/%d)",
                failed_provider,
                [a.provider_name for a in remaining],
                attempt,
                max_attempts,
            )
            yield StreamChunk(delta="", finish_reason="failover_restart", elapsed_ms=0)

        raise NoProvidersAvailableError(
            f"All providers failed mid-stream after {max_attempts} attempts."
        )
