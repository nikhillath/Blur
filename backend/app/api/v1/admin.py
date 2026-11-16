import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import AuditLog, RefreshToken, User
from app.schemas.auth import AuditLogOut, Page, Role, UserOut
from app.security.deps import get_current_user, require_admin
from app.security.passwords import hash_password, validate_password_policy
from app.security.tokens import utcnow
from app.services.audit import log_event

router = APIRouter(tags=["admin"])


# ─── list users ─────────────────────────────────────────────────────────────────
@router.get("/users", response_model=Page[UserOut])
def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Page[UserOut]:
    total = db.scalar(select(func.count()).select_from(User)) or 0
    rows = db.scalars(
        select(User).order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return Page(items=rows, total=total, page=page, page_size=page_size)


# ─── admin create user (gap 4a: needed when ALLOW_REGISTRATION=false) ───────────
class AdminCreateUser(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    role: Role = "USER"


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def admin_create_user(
    body: AdminCreateUser,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> User:
    """Admin-only: create a user account (works even when ALLOW_REGISTRATION is false)."""
    policy_error = validate_password_policy(body.password, body.email)
    if policy_error:
        raise HTTPException(status_code=422, detail=policy_error)
    user = User(
        name=body.name.strip(),
        email=body.email.strip().lower(),
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="email already registered")
    log_event(db, "ADMIN_USER_CREATED", user_id=admin.id,
              details={"new_user_id": str(user.id), "role": body.role}, request=request)
    db.commit()
    return user


# ─── update user role / active / password ───────────────────────────────────────
class AdminUserUpdate(BaseModel):
    role: Role | None = None
    is_active: bool | None = None
    new_password: str | None = Field(None, min_length=10, max_length=128)
    unlock: bool | None = None  # set True to clear lockout


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID,
    body: AdminUserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    if user.id == admin.id and (body.role not in (None, "ADMIN") or body.is_active is False):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot demote or disable yourself")
    changes: dict = {}
    if body.role is not None and body.role != user.role:
        changes["role"] = {"from": user.role, "to": body.role}
        user.role = body.role
    if body.is_active is not None and body.is_active != user.is_active:
        changes["is_active"] = body.is_active
        user.is_active = body.is_active
        if not body.is_active:
            now = utcnow()
            for t in db.scalars(
                select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            ):
                t.revoked_at = now
    if body.new_password is not None:
        # Admin password reset (gap 4c)
        policy_error = validate_password_policy(body.new_password, user.email)
        if policy_error:
            raise HTTPException(status_code=422, detail=policy_error)
        user.password_hash = hash_password(body.new_password)
        # Revoke all existing sessions so the old password can't be re-used via a cached token.
        now = utcnow()
        for t in db.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))):
            t.revoked_at = now
        changes["password_reset"] = True
    if body.unlock:
        user.failed_logins = 0
        user.locked_until = None
        changes["unlocked"] = True
    if changes:
        log_event(
            db, "ADMIN_USER_UPDATED", user_id=admin.id,
            details={"target_user": str(user.id), **changes}, request=request,
        )
    db.commit()
    return user


# ─── audit log ─────────────────────────────────────────────────────────────────
@router.get("/audit-logs", response_model=Page[AuditLogOut])
def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: str | None = None,
    user_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Page[AuditLogOut]:
    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action == action)
    if user_id:
        q = q.where(AuditLog.user_id == user_id)
    if document_id:
        q = q.where(AuditLog.document_id == document_id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(
        q.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return Page(items=rows, total=total, page=page, page_size=page_size)


# ─── audit chain verification ──────────────────────────────────────────────────
@router.get("/audit-logs/verify")
def verify_audit_chain(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    """Re-computes the hash chain for all linked rows and returns the verification result.
    A FAILED result means at least one row was altered or deleted out of order."""
    from app.services.audit import _compute_row_hash

    rows = db.scalars(
        select(AuditLog).where(AuditLog.seq.is_not(None)).order_by(AuditLog.seq)
    ).all()

    if not rows:
        return {"status": "ok", "rows_checked": 0, "message": "no chained rows found"}

    failures = []
    prev_hash = rows[0].prev_hash or "0" * 64

    for row in rows:
        expected = _compute_row_hash(prev_hash, row)
        if row.row_hash != expected:
            failures.append({"seq": row.seq, "id": str(row.id), "action": row.action})
        prev_hash = row.row_hash or expected  # continue with what we computed

    if failures:
        return {"status": "failed", "rows_checked": len(rows), "failures": failures}
    return {"status": "ok", "rows_checked": len(rows)}
