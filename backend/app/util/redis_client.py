"""Shared Redis connection factory.

Redis backs the RQ queue, worker coordination, and rate limiting (spec 7.2).
Connection failures are surfaced to callers so each subsystem can decide
whether to degrade or fail; nothing here retries silently.
"""

from __future__ import annotations

from functools import lru_cache

import redis

from app.config import get_settings


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    settings = get_settings()
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=False,
        socket_timeout=5,
        socket_connect_timeout=5,
        health_check_interval=30,
    )


def reset_redis_cache() -> None:
    """Test hook."""
    get_redis.cache_clear()


def redis_available() -> bool:
    try:
        return bool(get_redis().ping())
    except Exception:  # noqa: BLE001 - availability probe
        return False
