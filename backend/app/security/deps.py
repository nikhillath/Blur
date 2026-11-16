import uuid
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import RefreshToken, User
from app.security.tokens import decode_access_token, utcnow

_bearer = HTTPBearer(auto_error=False)
_UNAUTH = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """Identity comes only from the verified token; the user row is reloaded each request
    so disabled accounts and role changes take effect immediately."""
    if creds is None:
        raise _UNAUTH
    try:
        payload = decode_access_token(creds.credentials)
        user_id = uuid.UUID(payload["sub"])
        session_id = uuid.UUID(payload["sid"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise _UNAUTH
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _UNAUTH
    # The token carries its login family; it is valid while that family still has a live refresh session.
    live = db.scalar(select(exists().where(
        RefreshToken.family_id == session_id, RefreshToken.user_id == user.id,
        RefreshToken.revoked_at.is_(None), RefreshToken.expires_at > utcnow())))
    if not live:
        raise _UNAUTH  # logged out, expired or revoked
    return user


def require_roles(*roles: str) -> Callable[[User], User]:
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient permissions")
        return user

    return checker


require_admin = require_roles("ADMIN")
require_reviewer = require_roles("REVIEWER", "ADMIN")
