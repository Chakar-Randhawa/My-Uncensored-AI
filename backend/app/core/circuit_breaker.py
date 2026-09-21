"""
Circuit breaker — now pluggable between two backends:

  * `InMemoryCircuitBreaker` — process-local dict. Fine for a single Render
    free-tier instance, but state resets on every redeploy/restart and
    doesn't share across instances.
  * `RedisCircuitBreaker` — backed by Upstash Redis's REST API (works over
    plain HTTPS via httpx, no TCP connection pooling needed, and Upstash's
    free tier is generous enough for this workload). State survives
    restarts and is shared across every backend instance.

`build_circuit_breaker(settings)` picks Redis automatically when
`UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` are configured, and
falls back to in-memory otherwise — so the system still runs on a
free-tier-only, single-instance setup with zero extra signup required.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import httpx


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerProtocol(Protocol):
    async def is_available(self, provider: str) -> bool: ...
    async def record_success(self, provider: str) -> None: ...
    async def record_failure(self, provider: str) -> None: ...
    async def snapshot(self) -> dict[str, dict[str, object]]: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _BreakerRecord:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None


class InMemoryCircuitBreaker:
    def __init__(self, *, failure_threshold: int, reset_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._records: dict[str, _BreakerRecord] = {}
        self._lock = asyncio.Lock()

    async def is_available(self, provider: str) -> bool:
        async with self._lock:
            record = self._records.setdefault(provider, _BreakerRecord())

            if record.state == CircuitState.CLOSED:
                return True
            if record.state == CircuitState.OPEN:
                assert record.opened_at is not None
                if (time.monotonic() - record.opened_at) >= self._reset_seconds:
                    record.state = CircuitState.HALF_OPEN
                    return True
                return False
            return True  # HALF_OPEN: allow one probe through

    async def record_success(self, provider: str) -> None:
        async with self._lock:
            record = self._records.setdefault(provider, _BreakerRecord())
            record.state = CircuitState.CLOSED
            record.consecutive_failures = 0
            record.opened_at = None

    async def record_failure(self, provider: str) -> None:
        async with self._lock:
            record = self._records.setdefault(provider, _BreakerRecord())
            record.consecutive_failures += 1

            if record.state == CircuitState.HALF_OPEN:
                record.state = CircuitState.OPEN
                record.opened_at = time.monotonic()
                return

            if record.consecutive_failures >= self._failure_threshold:
                record.state = CircuitState.OPEN
                record.opened_at = time.monotonic()

    async def snapshot(self) -> dict[str, dict[str, object]]:
        return {
            provider: {"state": r.state.value, "consecutive_failures": r.consecutive_failures}
            for provider, r in self._records.items()
        }


# ---------------------------------------------------------------------------
# Upstash Redis REST implementation
# ---------------------------------------------------------------------------
class RedisCircuitBreaker:
    """
    Same state machine as `InMemoryCircuitBreaker`, persisted in Redis via
    Upstash's REST API so it survives restarts and is shared across every
    backend instance. Each provider gets a Redis hash `breaker:{provider}`
    with fields `state`, `failures`, `opened_at`.
    """

    def __init__(
        self,
        *,
        rest_url: str,
        rest_token: str,
        failure_threshold: int,
        reset_seconds: float,
    ) -> None:
        self._base_url = rest_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {rest_token}"}
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds

    async def _cmd(self, *parts: str) -> object:
        """Issue one Redis command via Upstash's path-based REST API."""
        url = f"{self._base_url}/" + "/".join(parts)
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers=self._headers)
            response.raise_for_status()
            return response.json().get("result")

    async def _get_hash(self, provider: str) -> dict[str, str]:
        raw = await self._cmd("hgetall", f"breaker:{provider}")
        if not raw:
            return {}
        # Upstash returns a flat [field, value, field, value, ...] array.
        return {raw[i]: raw[i + 1] for i in range(0, len(raw), 2)}

    async def is_available(self, provider: str) -> bool:
        try:
            fields = await self._get_hash(provider)
        except httpx.HTTPError:
            # Redis unreachable — fail open rather than blocking every race.
            return True

        state = fields.get("state", CircuitState.CLOSED.value)
        if state == CircuitState.CLOSED.value:
            return True
        if state == CircuitState.OPEN.value:
            opened_at = float(fields.get("opened_at", "0"))
            if (time.time() - opened_at) >= self._reset_seconds:
                await self._cmd("hset", f"breaker:{provider}", "state", CircuitState.HALF_OPEN.value)
                return True
            return False
        return True  # HALF_OPEN probe

    async def record_success(self, provider: str) -> None:
        try:
            await self._cmd(
                "hset", f"breaker:{provider}",
                "state", CircuitState.CLOSED.value,
                "failures", "0",
            )
        except httpx.HTTPError:
            pass

    async def record_failure(self, provider: str) -> None:
        try:
            fields = await self._get_hash(provider)
            state = fields.get("state", CircuitState.CLOSED.value)
            failures = int(fields.get("failures", "0")) + 1

            if state == CircuitState.HALF_OPEN.value:
                await self._cmd(
                    "hset", f"breaker:{provider}",
                    "state", CircuitState.OPEN.value,
                    "opened_at", str(time.time()),
                    "failures", str(failures),
                )
                return

            new_state = state
            extra: list[str] = []
            if failures >= self._failure_threshold:
                new_state = CircuitState.OPEN.value
                extra = ["opened_at", str(time.time())]

            await self._cmd(
                "hset", f"breaker:{provider}",
                "state", new_state,
                "failures", str(failures),
                *extra,
            )
        except httpx.HTTPError:
            pass

    async def snapshot(self) -> dict[str, dict[str, object]]:
        try:
            keys = await self._cmd("keys", "breaker:*")
        except httpx.HTTPError:
            return {}
        result: dict[str, dict[str, object]] = {}
        for key in keys or []:
            provider = key.split("breaker:", 1)[-1]
            fields = await self._get_hash(provider)
            result[provider] = {
                "state": fields.get("state", CircuitState.CLOSED.value),
                "consecutive_failures": int(fields.get("failures", "0")),
            }
        return result


def build_circuit_breaker(
    *,
    failure_threshold: int,
    reset_seconds: float,
    upstash_rest_url: str | None,
    upstash_rest_token: str | None,
) -> CircuitBreakerProtocol:
    if upstash_rest_url and upstash_rest_token:
        return RedisCircuitBreaker(
            rest_url=upstash_rest_url,
            rest_token=upstash_rest_token,
            failure_threshold=failure_threshold,
            reset_seconds=reset_seconds,
        )
    return InMemoryCircuitBreaker(failure_threshold=failure_threshold, reset_seconds=reset_seconds)
