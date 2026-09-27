"""Key encryption, tamper detection, and startup validation (spec 5.2, 10.4, 12.1)."""

from __future__ import annotations

import base64
import os

import pytest

from app.config import ConfigurationError, Settings
from app.security.crypto import (
    NONCE_BYTES,
    TAG_BYTES,
    DecryptionError,
    EncryptedSecret,
    decrypt_secret,
    encrypt_secret,
)
from app.versions import ENCRYPTION_FORMAT_VERSION

MASTER = b"\x11" * 32
OTHER_MASTER = b"\x22" * 32
RECORD = "0192f000-0000-7000-8000-000000000001"
SECRET = "AIzaSyFAKEKEYSENTINEL_do_not_log_0000000"


class TestRoundTrip:
    def test_encrypt_then_decrypt(self):
        sealed = encrypt_secret(SECRET, master_key=MASTER, record_id=RECORD)
        assert decrypt_secret(sealed, master_key=MASTER, record_id=RECORD) == SECRET

    def test_envelope_shape(self):
        sealed = encrypt_secret(SECRET, master_key=MASTER, record_id=RECORD)
        assert sealed.version == ENCRYPTION_FORMAT_VERSION
        assert len(sealed.nonce) == NONCE_BYTES == 12
        assert len(sealed.tag) == TAG_BYTES == 16
        assert sealed.ciphertext

    def test_plaintext_never_appears_in_the_envelope(self):
        sealed = encrypt_secret(SECRET, master_key=MASTER, record_id=RECORD)
        blob = sealed.nonce + sealed.ciphertext + sealed.tag
        assert SECRET.encode() not in blob

    def test_every_write_uses_a_fresh_nonce(self):
        nonces = {
            encrypt_secret(SECRET, master_key=MASTER, record_id=RECORD).nonce
            for _ in range(200)
        }
        assert len(nonces) == 200

    def test_refuses_to_encrypt_an_empty_secret(self):
        with pytest.raises(ValueError):
            encrypt_secret("", master_key=MASTER, record_id=RECORD)


class TestTamperDetection:
    @pytest.fixture
    def sealed(self):
        return encrypt_secret(SECRET, master_key=MASTER, record_id=RECORD)

    def test_wrong_master_key_fails_safely(self, sealed):
        with pytest.raises(DecryptionError):
            decrypt_secret(sealed, master_key=OTHER_MASTER, record_id=RECORD)

    def test_wrong_record_id_fails_safely(self, sealed):
        # The AAD binds a ciphertext to its settings record.
        with pytest.raises(DecryptionError):
            decrypt_secret(sealed, master_key=MASTER, record_id="different-record")

    def test_tampered_ciphertext_fails(self, sealed):
        broken = EncryptedSecret(
            sealed.version, sealed.nonce, _flip(sealed.ciphertext), sealed.tag
        )
        with pytest.raises(DecryptionError):
            decrypt_secret(broken, master_key=MASTER, record_id=RECORD)

    def test_tampered_nonce_fails(self, sealed):
        broken = EncryptedSecret(
            sealed.version, _flip(sealed.nonce), sealed.ciphertext, sealed.tag
        )
        with pytest.raises(DecryptionError):
            decrypt_secret(broken, master_key=MASTER, record_id=RECORD)

    def test_tampered_tag_fails(self, sealed):
        broken = EncryptedSecret(
            sealed.version, sealed.nonce, sealed.ciphertext, _flip(sealed.tag)
        )
        with pytest.raises(DecryptionError):
            decrypt_secret(broken, master_key=MASTER, record_id=RECORD)

    def test_tampered_version_fails(self, sealed):
        broken = EncryptedSecret(
            sealed.version + 1, sealed.nonce, sealed.ciphertext, sealed.tag
        )
        with pytest.raises(DecryptionError):
            decrypt_secret(broken, master_key=MASTER, record_id=RECORD)

    def test_truncated_nonce_fails(self, sealed):
        broken = EncryptedSecret(
            sealed.version, sealed.nonce[:-1], sealed.ciphertext, sealed.tag
        )
        with pytest.raises(DecryptionError):
            decrypt_secret(broken, master_key=MASTER, record_id=RECORD)

    def test_error_message_does_not_identify_the_broken_component(self, sealed):
        broken = EncryptedSecret(sealed.version, sealed.nonce, sealed.ciphertext, _flip(sealed.tag))
        with pytest.raises(DecryptionError) as caught:
            decrypt_secret(broken, master_key=MASTER, record_id=RECORD)
        assert "tag" not in str(caught.value).lower()
        assert "nonce" not in str(caught.value).lower()

    def test_short_master_key_is_rejected(self, sealed):
        with pytest.raises(DecryptionError):
            decrypt_secret(sealed, master_key=b"\x00" * 16, record_id=RECORD)


def _flip(data: bytes) -> bytes:
    mutated = bytearray(data)
    mutated[0] ^= 0xFF
    return bytes(mutated)


class TestStartupValidation:
    """The app must refuse to start when secrets are missing or malformed."""

    def _settings(self, **overrides):
        base = {
            "app_encryption_key": base64.b64encode(os.urandom(32)).decode(),
        }
        base.update(overrides)
        return Settings(_env_file=None, **base)

    def test_valid_configuration_loads(self):
        assert len(self._settings().encryption_key_bytes) == 32

    @pytest.mark.parametrize(
        "override",
        [
            {"app_encryption_key": ""},
            {"app_encryption_key": "not-base64!!"},
            {"app_encryption_key": base64.b64encode(b"short").decode()},
        ],
    )
    def test_invalid_configuration_is_refused(self, override):
        with pytest.raises(Exception):
            self._settings(**override)

    def test_encryption_key_decodes_to_32_bytes(self):
        assert len(self._settings().encryption_key_bytes) == 32

    def test_allowed_origin_is_scheme_and_host_only(self):
        settings = self._settings(app_base_url="https://clips.example.internal/app?x=1")
        assert settings.allowed_origin == "https://clips.example.internal"

    def test_non_absolute_base_url_is_rejected(self):
        settings = self._settings(app_base_url="clips.example.internal")
        with pytest.raises(ConfigurationError):
            _ = settings.allowed_origin

    def test_loopback_detection_controls_cookie_secure(self):
        assert self._settings(app_base_url="http://localhost:5173").is_loopback
        assert not self._settings(app_base_url="https://clips.example.internal").is_loopback
