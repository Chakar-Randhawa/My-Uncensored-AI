from __future__ import annotations

import asyncio

import pytest

from app.core.circuit_breaker import InMemoryCircuitBreaker
from app.core.racing_engine import NoProvidersAvailableError, RacingEngine
from app.models.schemas import ChatMessage, ChatRole
from tests.conftest import FakeAdapter

MESSAGES = [ChatMessage(role=ChatRole.USER, content="hi")]


def make_engine(*, race_timeout: float = 2.0) -> RacingEngine:
    breaker = InMemoryCircuitBreaker(failure_threshold=3, reset_seconds=60)
    return RacingEngine(circuit_breaker=breaker, race_timeout_seconds=race_timeout)


async def test_fastest_provider_wins_and_slower_one_is_cancelled():
    engine = make_engine()
    fast = FakeAdapter("fast", delay=0.02, tokens=["Hello", " ", "World"])
    slow = FakeAdapter("slow", delay=0.5, tokens=["Should", "not", "appear"])

    output = [chunk.delta async for chunk in engine.race([slow, fast], MESSAGES, temperature=0.7, max_tokens=100)]

    assert engine.last_result.winner == "fast"
    assert "".join(output) == "Hello World"

    await asyncio.sleep(0.1)  # let the cancellation propagate
    assert slow.cancelled is True


async def test_no_providers_available_raises():
    engine = make_engine(race_timeout=0.3)
    stalled = FakeAdapter("stalled", never_respond=True)

    with pytest.raises(NoProvidersAvailableError):
        async for _ in engine.race([stalled], MESSAGES, temperature=0.7, max_tokens=100):
            pass


async def test_circuit_breaker_open_provider_is_excluded():
    breaker = InMemoryCircuitBreaker(failure_threshold=1, reset_seconds=60)
    engine = RacingEngine(circuit_breaker=breaker, race_timeout_seconds=1.0)
    await breaker.record_failure("broken")

    working = FakeAdapter("broken", tokens=["should not run"])
    with pytest.raises(NoProvidersAvailableError):
        async for _ in engine.race([working], MESSAGES, temperature=0.7, max_tokens=100):
            pass


async def test_failover_retries_with_next_provider_after_mid_stream_error():
    engine = make_engine()
    dying = FakeAdapter("dying", delay=0.01, tokens=["Partial", "answer"], fail_at_token=1)
    backup = FakeAdapter("backup", delay=0.05, tokens=["Full", " ", "answer"])

    events = [
        chunk
        async for chunk in engine.race_with_failover(
            [dying, backup], MESSAGES, temperature=0.7, max_tokens=100
        )
    ]

    restart_events = [c for c in events if c.finish_reason == "failover_restart"]
    assert len(restart_events) == 1

    # Everything yielded *after* the restart marker should be from the
    # backup provider, not a mix of both.
    restart_index = events.index(restart_events[0])
    post_restart_text = "".join(c.delta for c in events[restart_index + 1 :] if c.delta)
    assert post_restart_text == "Full answer"


async def test_failover_exhausts_all_providers_and_raises():
    engine = make_engine()
    # fail_at_token=1 (not 0): each fake must yield one real token first so
    # it actually wins its (single-entrant) attempt before failing — a
    # provider that errors before ever producing a token never becomes a
    # winner in the first place, so the racing engine would just time out
    # waiting for *someone* to win rather than reporting a mid-stream error.
    dying_a = FakeAdapter("dying_a", tokens=["x", "x2"], fail_at_token=1, token_interval=0.0)
    dying_b = FakeAdapter("dying_b", tokens=["y", "y2"], fail_at_token=1, token_interval=0.0)

    with pytest.raises(NoProvidersAvailableError):
        async for _ in engine.race_with_failover(
            [dying_a, dying_b], MESSAGES, temperature=0.7, max_tokens=100, max_attempts=5
        ):
            pass
