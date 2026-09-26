"""UUIDv7 generation (spec 8.1: resource IDs are UUIDv7).

RFC 9562 layout: 48-bit big-endian Unix epoch milliseconds, 4-bit version,
12 bits of randomness, 2-bit variant, 62 bits of randomness. Monotonicity
within a millisecond is provided by a per-process counter seeded from the
random block, which keeps IDs sortable when many are minted in one batch.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = -1
_last_seq = 0

_MAX_SEQ = 0xFFF


def uuid7() -> uuid.UUID:
    global _last_ms, _last_seq

    with _lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms == _last_ms:
            if _last_seq >= _MAX_SEQ:
                # Sequence space exhausted inside this millisecond; advance the
                # timestamp rather than emitting a non-monotonic value.
                now_ms += 1
                _last_ms = now_ms
                _last_seq = 0
            else:
                _last_seq += 1
        elif now_ms < _last_ms:
            # Clock moved backwards: keep issuing from the last observed ms.
            now_ms = _last_ms
            _last_seq = min(_last_seq + 1, _MAX_SEQ)
        else:
            _last_ms = now_ms
            _last_seq = int.from_bytes(os.urandom(2), "big") & _MAX_SEQ
        seq = _last_seq

    tail = int.from_bytes(os.urandom(8), "big")
    value = (now_ms & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76
    value |= (seq & _MAX_SEQ) << 64
    value |= (0b10 << 62) | (tail & 0x3FFF_FFFF_FFFF_FFFF)
    return uuid.UUID(int=value)


def new_id() -> str:
    """Canonical string form used in the API and database."""
    return str(uuid7())


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True
