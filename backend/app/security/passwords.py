"""Argon2id password verification for the shared administrator account.

The deployment is initialized with a PHC hash string, never a plaintext
password (spec 5.1). Verification is always performed against a hash, and a
dummy verification runs when no hash is configured so that failure timing does
not distinguish "no account" from "wrong password".
"""

from __future__ import annotations

import hmac

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

# Interactive-login parameters: ~64 MiB, 3 passes, 4 lanes.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

# Used to equalize timing when the configured hash is unusable.
_DUMMY_HASH = _hasher.hash("timing-equalization-placeholder")


def hash_password(password: str) -> str:
    """Produce an Argon2id PHC string for ``ADMIN_PASSWORD_HASH``."""
    if not password:
        raise ValueError("Refusing to hash an empty password.")
    return _hasher.hash(password)


def verify_password(password: str, encoded_hash: str) -> bool:
    """Verify a candidate password in constant-ish time."""
    target = encoded_hash if encoded_hash else _DUMMY_HASH
    try:
        _hasher.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return bool(encoded_hash)


def verify_username(candidate: str, expected: str) -> bool:
    """Compare usernames without leaking length or content through timing."""
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
