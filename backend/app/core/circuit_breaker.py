"""
Minimal, dependency-free circuit breaker, one instance per provider.

States:
    CLOSED     — normal operation, provider enters every race.
    OPEN       — provider tripped `failure_threshold` consecutive errors;
                 skipped entirely until `reset_seconds` has elapsed.
    HALF_OPEN  — cooldown elapsed; the *next* race lets this provider back
                 in as a probe. One more failure re-opens it immediately;
                 one success closes it and resets the failure count.

This is process-local state (an in-memory dict), which is the right choice
for a free-tier, single-instance Render deployment — it survives for the
life of the process and resets on redeploy, which is acceptable since the
provider's own rate limit window resets on a similar cadence anyway.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class _BreakerRecord:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int, reset_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._records: dict[str, _BreakerRecord] = {}
        self._lock = asyncio.Lock()

    async def is_available(self, provider: str) -> bool:
        """Whether `provider` should be entered into the next race."""
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

            # HALF_OPEN: allow exactly one probing attempt through. We don't
            # flip state here — record_success/record_failure decides the
            # outcome once the probe completes.
            return True

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
                # Probe failed — snap straight back open.
                record.state = CircuitState.OPEN
                record.opened_at = time.monotonic()
                return

            if record.consecutive_failures >= self._failure_threshold:
                record.state = CircuitState.OPEN
                record.opened_at = time.monotonic()

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Diagnostic view for a /health or /admin endpoint."""
        return {
            provider: {
                "state": record.state.value,
                "consecutive_failures": record.consecutive_failures,
            }
            for provider, record in self._records.items()
        }
