from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Per-project token bucket, in-process only: it resets on restart and is not shared
    between worker processes (documented limitation; revisit in M5)."""

    def __init__(
        self,
        burst: int,
        per_second: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._burst = float(burst)
        self._rate = per_second
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str, cost: int) -> float | None:
        """Returns None when allowed, else seconds to wait before retrying."""
        cost_f = min(float(cost), self._burst)
        with self._lock:
            now = self._clock()
            bucket = self._buckets.setdefault(key, _Bucket(self._burst, now))
            bucket.tokens = min(self._burst, bucket.tokens + (now - bucket.updated) * self._rate)
            bucket.updated = now
            if bucket.tokens >= cost_f:
                bucket.tokens -= cost_f
                return None
            return (cost_f - bucket.tokens) / self._rate if self._rate > 0 else 60.0
