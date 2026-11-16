"""Application-level encryption of sensitive data at rest (Fernet = AES-128-CBC + HMAC-SHA256).

* ``ENCRYPTION_KEYS`` is a comma-separated list of Fernet keys. The FIRST key encrypts; all keys can decrypt, which is
  how keys are rotated (add a new key in front, run ``python -m scripts.encrypt_existing --rotate``, later drop the old one).
* Encrypted database values carry the prefix ``enc1:``; encrypted files start with the magic bytes ``PIISENC1``.
  Anything without the marker is treated as legacy plaintext and returned unchanged, so existing data keeps working
  until ``scripts.encrypt_existing`` converts it.
* With no keys configured encryption is OFF (allowed only in development; production refuses to start).
"""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings

TEXT_PREFIX = "enc1:"
FILE_MAGIC = b"PIISENC1"


class DecryptionError(Exception):
    """The data is encrypted but none of the configured keys can open it."""


@lru_cache
def _cipher(keys: str) -> MultiFernet | None:
    parts = [k.strip() for k in keys.split(",") if k.strip()]
    return MultiFernet([Fernet(k.encode()) for k in parts]) if parts else None


def cipher() -> MultiFernet | None:
    return _cipher(get_settings().encryption_keys)


def enabled() -> bool:
    return cipher() is not None


def generate_key() -> str:
    return Fernet.generate_key().decode()


def encrypt_str(value: str) -> str:
    c = cipher()
    if c is None or value.startswith(TEXT_PREFIX):
        return value
    return TEXT_PREFIX + c.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_str(value: str) -> str:
    if not value.startswith(TEXT_PREFIX):
        return value  # legacy plaintext
    c = cipher()
    if c is None:
        raise DecryptionError("encrypted data found but no ENCRYPTION_KEYS are configured")
    try:
        return c.decrypt(value[len(TEXT_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise DecryptionError("no configured key can decrypt this value") from exc


def encrypt_bytes(data: bytes) -> bytes:
    c = cipher()
    if c is None or data.startswith(FILE_MAGIC):
        return data
    return FILE_MAGIC + c.encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    if not data.startswith(FILE_MAGIC):
        return data
    c = cipher()
    if c is None:
        raise DecryptionError("encrypted file found but no ENCRYPTION_KEYS are configured")
    try:
        return c.decrypt(data[len(FILE_MAGIC):])
    except InvalidToken as exc:
        raise DecryptionError("no configured key can decrypt this file") from exc


def is_encrypted_text(value: str | None) -> bool:
    return bool(value) and value.startswith(TEXT_PREFIX)


class EncryptedText(TypeDecorator):
    """Text column that is encrypted on write and decrypted on read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else encrypt_str(value)

    def process_result_value(self, value, dialect):
        return None if value is None else decrypt_str(value)
