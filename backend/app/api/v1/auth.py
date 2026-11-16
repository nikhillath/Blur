import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.ratelimit import limiter
from app.db.session import get_db
from app.models import RefreshToken, User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.security.deps import get_current_user
from app.security.passwords import (
    burn_verify,
    hash_password,
    needs_rehash,
    verify_password,
    validate_password_policy,
)
from app.security.tokens import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
    utcnow,
)
from app.services.audit import log_event

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = "pii_refresh"
COOKIE_PATH = "/api/v1/auth"
_RATE = get_settings().auth_rate_limit


def require_trusted_origin(request: Request) -> None:
    """Cookie-authenticated endpoints refuse browser requests that come from another site (CSRF defence in
    depth on top of SameSite). Non-browser clients send no Origin header and are unaffected."""
    origin = request.headers.get("origin")
    if origin and origin not in get_settings().cors_origin_list:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="untrusted origin")


def _set_refresh_cookie(response: Response, raw: str) -> None:
    s = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        raw,
        max_age=s.refresh_token_days * 86400,
        httponly=True,
        secure=s.environment != "development",
        samesite="lax",
        path=COOKIE_PATH,
    )


REUSE_GRACE = timedelta(seconds=10)  # two tabs refreshing at the same moment are not an attack


def _issue_tokens(db: Session, user: User, response: Response, family_id: uuid.UUID | None = None) -> TokenResponse:
    raw, token_hash, expires = new_refresh_token()
    session = RefreshToken(user_id=user.id, token_hash=token_hash, expires_at=expires, family_id=family_id or uuid.uuid4())
    db.add(session)
    db.flush()
    access, ttl = create_access_token(user.id, user.role, session.family_id)
    _set_refresh_cookie(response, raw)
    return TokenResponse(access_token=access, expires_in=ttl, user=UserOut.model_validate(user))


def _revoke_all_sessions(db: Session, user_id: uuid.UUID) -> None:
    """Revoke every live refresh session for a user (used on lockout & password change)."""
    now = utcnow()
    for t in db.scalars(select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))):
        t.revoked_at = now


# ── GET /auth/config ────────────────────────────────────────────────────────────
@router.get("/config")
def auth_config() -> dict:
    """Returns public flags the frontend uses (e.g. to hide the Register button when registration is closed)."""
    return {"registration_open": get_settings().allow_registration}


# ── POST /auth/register ─────────────────────────────────────────────────────────
@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(_RATE)
def register(
    request: Request, body: RegisterRequest, db: Session = Depends(get_db)
) -> User:
    if not get_settings().allow_registration:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="registration is closed")
    policy_error = validate_password_policy(body.password, body.email)
    if policy_error:
        raise HTTPException(status_code=422, detail=policy_error)
    user = User(
        name=body.name,
        email=body.email,
        password_hash=hash_password(body.password),
        role="USER",  # role is never client-controlled
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered")
    log_event(db, "USER_REGISTERED", user_id=user.id, request=request)
    db.commit()
    return user


# ── POST /auth/login ────────────────────────────────────────────────────────────
@router.post("/login", response_model=TokenResponse)
@limiter.limit(_RATE)
def login(
    request: Request, response: Response, body: LoginRequest, db: Session = Depends(get_db)
) -> TokenResponse:
    s = get_settings()
    user = db.scalar(select(User).where(User.email == body.email))
    if user is None:
        burn_verify(body.password)

    # ── account lockout check ───────────────────────────────────────────────
    now = utcnow()
    if user is not None and user.locked_until is not None and user.locked_until > now:
        log_event(db, "LOGIN_FAILED", user_id=user.id, details={"reason": "account_locked"}, request=request)
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account is locked; try again later")

    ok = user is not None and verify_password(body.password, user.password_hash)
    if not ok or (user is not None and not user.is_active):
        if user is not None:
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= s.max_failed_logins:
                user.locked_until = now + timedelta(minutes=s.lockout_minutes)
                _revoke_all_sessions(db, user.id)
                log_event(db, "ACCOUNT_LOCKED", user_id=user.id,
                          details={"failed_logins": user.failed_logins}, request=request)
        log_event(db, "LOGIN_FAILED", user_id=user.id if user else None, request=request)
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    # ── success: reset lockout counters ────────────────────────────────────
    user.failed_logins = 0
    user.locked_until = None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    result = _issue_tokens(db, user, response)
    log_event(db, "LOGIN_SUCCEEDED", user_id=user.id, request=request)
    db.commit()
    return result


# ── POST /auth/refresh ──────────────────────────────────────────────────────────
@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(_RATE)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)) -> TokenResponse:
    require_trusted_origin(request)
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="no refresh session")
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw)))
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh session")
    now = utcnow()
    if row.revoked_at is not None:
        if now - row.revoked_at < REUSE_GRACE:  # benign race: reject, but do not punish the session
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh in progress, retry")
        # A long-rotated token came back: assume theft and end this login family (other devices are unaffected).
        for t in db.scalars(select(RefreshToken).where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))):
            t.revoked_at = now
        log_event(db, "REFRESH_REUSE_DETECTED", user_id=row.user_id, request=request)
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh session")
    user = db.get(User, row.user_id)
    if row.expires_at <= now or user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh session")
    row.revoked_at = now  # rotation
    result = _issue_tokens(db, user, response, row.family_id)
    db.commit()
    return result


# ── POST /auth/logout ───────────────────────────────────────────────────────────
@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> Response:
    require_trusted_origin(request)
    raw = request.cookies.get(COOKIE_NAME)
    if raw:
        row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw)))
        if row is not None and row.revoked_at is None:
            for t in db.scalars(select(RefreshToken).where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))):
                t.revoked_at = utcnow()
            log_event(db, "LOGOUT", user_id=row.user_id, request=request)
            db.commit()
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


# ── GET /auth/me ────────────────────────────────────────────────────────────────
@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user


# ── POST /auth/change-password ──────────────────────────────────────────────────
@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(_RATE)
def change_password(
    request: Request,
    response: Response,
    body: ChangePasswordRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Allows a user to change their own password.  Revokes all existing sessions and issues new tokens."""
    if not verify_password(body.current_password, user.password_hash):
        log_event(db, "CHANGE_PASSWORD_FAILED", user_id=user.id, request=request)
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="current password is wrong")
    policy_error = validate_password_policy(body.new_password, user.email)
    if policy_error:
        raise HTTPException(status_code=422, detail=policy_error)
    _revoke_all_sessions(db, user.id)
    user.password_hash = hash_password(body.new_password)
    # Issue fresh tokens so the user is not unexpectedly signed out.
    _issue_tokens(db, user, response)
    log_event(db, "PASSWORD_CHANGED", user_id=user.id, request=request)
    db.commit()
