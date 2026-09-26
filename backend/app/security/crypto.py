"""AES-256-GCM envelope encryption for provider API keys (spec 5.2).

Only the encryption-format version, nonce, ciphertext, and authentication tag
are persisted. The 32-byte master key lives exclusively in the environment and
is never written to SQLite. Authenticated additional data binds each ciphertext
to the settings-record identifier and the format version, so a ciphertext
cannot be replayed into a different record or reinterpreted under a future
format.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.versions import ENCRYPTION_FORMAT_VERSION

NONCE_BYTES = 12  # 96-bit nonce, the GCM-recommended size.
TAG_BYTES = 16
MASTER_KEY_BYTES = 32


class DecryptionError(Exception):
    """Raised when ciphertext, nonce, tag, master key, or AAD do not agree.

    The message is deliberately generic: a decryption failure must never
    disclose which component was tampered with.
    """

    def __init__(self, message: str = "Stored secret could not be decrypted.") -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    """The persisted envelope. Contains no key material of its own."""

    version: int
    nonce: bytes
    ciphertext: bytes
    tag: bytes


def _aad(record_id: str, version: int) -> bytes:
    """Authenticated metadata: settings-record identifier + format version."""
    return f"v{version}|{record_id}".encode("utf-8")


def _validate_master_key(master_key: bytes) -> None:
    if len(master_key) != MASTER_KEY_BYTES:
        raise DecryptionError("Deployment master key is not 32 bytes.")


def encrypt_secret(plaintext: str, *, master_key: bytes, record_id: str) -> EncryptedSecret:
    """Encrypt a secret under a fresh random nonce."""
    _validate_master_key(master_key)
    if not plaintext:
        raise ValueError("Refusing to encrypt an empty secret.")

    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(master_key).encrypt(
        nonce,
        plaintext.encode("utf-8"),
        _aad(record_id, ENCRYPTION_FORMAT_VERSION),
    )
    # cryptography appends the tag to the ciphertext; split so each component
    # is stored (and can be tamper-tested) independently.
    return EncryptedSecret(
        version=ENCRYPTION_FORMAT_VERSION,
        nonce=nonce,
        ciphertext=sealed[:-TAG_BYTES],
        tag=sealed[-TAG_BYTES:],
    )


def decrypt_secret(secret: EncryptedSecret, *, master_key: bytes, record_id: str) -> str:
    """Decrypt a persisted envelope, or fail safely.

    The plaintext is returned to the caller for immediate use in a single
    outbound provider call and must not be cached, logged, or enqueued.
    """
    _validate_master_key(master_key)
    if secret.version != ENCRYPTION_FORMAT_VERSION:
        raise DecryptionError()
    if len(secret.nonce) != NONCE_BYTES or len(secret.tag) != TAG_BYTES:
        raise DecryptionError()

    try:
        plaintext = AESGCM(master_key).decrypt(
            secret.nonce,
            secret.ciphertext + secret.tag,
            _aad(record_id, secret.version),
        )
    except InvalidTag as exc:
        raise DecryptionError() from exc
    except Exception as exc:  # noqa: BLE001 - never leak the underlying cause
        raise DecryptionError() from exc

    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecryptionError() from exc
