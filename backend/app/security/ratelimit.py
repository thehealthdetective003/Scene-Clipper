"""Fixed-window rate limiting for authentication and key-validation routes.

Spec 5.1/8.2/10.1 require throttling on login and Gemini key testing. The
counter lives in Redis so it is shared across API replicas; if Redis is
unreachable the limiter falls back to an in-process counter rather than
failing open, because an unlimited login endpoint is the worse outcome.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from app.util.redis_client import get_redis


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class _LocalWindows:
    """Process-local fallback with the same fixed-window semantics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, int], int] = {}

    def incr(self, key: str, window: int) -> int:
        with self._lock:
            slot = (key, window)
            count = self._counts.get(slot, 0) + 1
            self._counts[slot] = count
            if len(self._counts) > 4096:
                for stale in [k for k in self._counts if k[1] < window - 1]:
                    self._counts.pop(stale, None)
            return count


_local_windows = _LocalWindows()


def check_rate_limit(bucket: str, identity: str, limit: int, window_seconds: int = 60) -> RateLimitResult:
    """Consume one unit from ``bucket:identity`` for the current window."""
    if limit <= 0:
        return RateLimitResult(allowed=False, remaining=0, retry_after_seconds=window_seconds)

    now = int(time.time())
    window = now // window_seconds
    reset_in = window_seconds - (now % window_seconds)
    key = f"ratelimit:{bucket}:{identity}:{window}"

    try:
        client = get_redis()
        pipe = client.pipeline()
        pipe.incr(key, 1)
        pipe.expire(key, window_seconds + 1)
        count = int(pipe.execute()[0])
    except Exception:  # noqa: BLE001 - degrade to the local limiter
        count = _local_windows.incr(f"{bucket}:{identity}", window)

    if count > limit:
        return RateLimitResult(allowed=False, remaining=0, retry_after_seconds=reset_in)
    return RateLimitResult(allowed=True, remaining=limit - count, retry_after_seconds=reset_in)
