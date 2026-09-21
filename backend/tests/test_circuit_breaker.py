from __future__ import annotations

import asyncio

from app.core.circuit_breaker import CircuitState, InMemoryCircuitBreaker, build_circuit_breaker


async def test_opens_after_failure_threshold():
    breaker = InMemoryCircuitBreaker(failure_threshold=2, reset_seconds=60)

    assert await breaker.is_available("p") is True
    await breaker.record_failure("p")
    assert await breaker.is_available("p") is True  # 1 failure, threshold 2

    await breaker.record_failure("p")
    assert await breaker.is_available("p") is False  # now OPEN


async def test_half_open_probe_after_cooldown_then_closes_on_success():
    breaker = InMemoryCircuitBreaker(failure_threshold=1, reset_seconds=0.15)

    await breaker.record_failure("p")
    assert await breaker.is_available("p") is False

    await asyncio.sleep(0.2)
    assert await breaker.is_available("p") is True  # HALF_OPEN probe allowed

    await breaker.record_success("p")
    snapshot = await breaker.snapshot()
    assert snapshot["p"]["state"] == CircuitState.CLOSED.value
    assert snapshot["p"]["consecutive_failures"] == 0


async def test_half_open_probe_failure_reopens_immediately():
    breaker = InMemoryCircuitBreaker(failure_threshold=1, reset_seconds=0.1)

    await breaker.record_failure("p")
    await asyncio.sleep(0.15)
    assert await breaker.is_available("p") is True  # HALF_OPEN

    await breaker.record_failure("p")  # probe failed
    assert await breaker.is_available("p") is False  # snapped back OPEN


async def test_factory_falls_back_to_in_memory_without_upstash_config():
    breaker = build_circuit_breaker(
        failure_threshold=3,
        reset_seconds=60,
        upstash_rest_url=None,
        upstash_rest_token=None,
    )
    assert isinstance(breaker, InMemoryCircuitBreaker)
