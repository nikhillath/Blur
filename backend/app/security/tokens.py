import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import get_settings

ALGORITHM = "HS256"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(user_id: uuid.UUID, role: str, session_id: uuid.UUID) -> tuple[str, int]:
    s = get_settings()
    now = utcnow()
    expires = now + timedelta(minutes=s.access_token_minutes)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "sid": str(session_id),
        "iat": now,
        "exp": expires,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, s.secret_key, algorithm=ALGORITHM), s.access_token_minutes * 60


def decode_access_token(token: str) -> dict:
    """Raises jwt.PyJWTError on any invalid, expired, or wrong-type token."""
    payload = jwt.decode(
        token,
        get_settings().secret_key,
        algorithms=[ALGORITHM],
        options={"require": ["sub", "exp", "type", "sid"]},
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def new_refresh_token() -> tuple[str, str, datetime]:
    """Returns (raw_token, sha256_hash, expires_at). Only the hash is stored."""
    raw = secrets.token_urlsafe(48)
    expires = utcnow() + timedelta(days=get_settings().refresh_token_days)
    return raw, hash_refresh_token(raw), expires


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
