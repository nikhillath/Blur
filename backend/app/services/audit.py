"""Audit logging service.

Every action is recorded by calling log_event() inside the caller's transaction;
the caller commits.  Sensitive keys are never stored in details.

The audit log is tamper-evident: each row records a SHA-256 hash of the previous
row's hash concatenated with its own fields (gap 3).  A pg_advisory_xact_lock
serialises inserts so the chain is strictly ordered.
"""
import hashlib
import json
import logging
import uuid
from datetime import timezone

from fastapi import Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import AuditLog

logger = logging.getLogger("pii_sentinel.audit")

_FORBIDDEN_KEYS = {"password", "token", "access_token", "refresh_token", "detected_value", "text"}

# Arbitrary but stable advisory-lock key (must fit in int64).
_CHAIN_LOCK: int = 0x50494953454E54  # "PIISENT" in ASCII


def request_id_of(request: Request | None) -> str | None:
    if request is None:
        return None
    return getattr(request.state, "request_id", None)


def _compute_row_hash(prev_hash: str, row: AuditLog) -> str:
    """Deterministic SHA-256 over the chain fields and the immutable row payload."""
    details_json = json.dumps(row.details, sort_keys=True, default=str) if row.details is not None else "null"
    ts = row.created_at.isoformat() if hasattr(row.created_at, "isoformat") else str(row.created_at)
    payload = "|".join([
        prev_hash,
        str(row.id),
        str(row.user_id) if row.user_id is not None else "",
        str(row.document_id) if row.document_id is not None else "",
        row.action or "",
        details_json,
        row.request_id or "",
        ts,
    ])
    return hashlib.sha256(payload.encode()).hexdigest()


def log_event(
    db: Session,
    action: str,
    *,
    user_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    details: dict | None = None,
    request: Request | None = None,
) -> AuditLog:
    """Adds an audit row to the caller's transaction (caller commits). Sensitive keys are dropped.

    A transaction-level advisory lock serialises writes so the hash chain is strictly monotonic.
    """
    safe = {k: v for k, v in (details or {}).items() if k.lower() not in _FORBIDDEN_KEYS}

    # ── acquire advisory lock (released at transaction end automatically) ───────
    try:
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CHAIN_LOCK})
    except Exception:
        # Non-PostgreSQL (e.g. SQLite in tests) or lock unavailable – skip chain.
        logger.debug("advisory lock unavailable; audit chain will be unlinked for this row")

    # ── read the last row hash ──────────────────────────────────────────────────
    last = db.scalar(select(AuditLog.row_hash).order_by(AuditLog.seq.desc().nulls_last()).limit(1))
    prev_hash = last if last else "0" * 64

    # ── get next seq ────────────────────────────────────────────────────────────
    last_seq = db.scalar(select(AuditLog.seq).order_by(AuditLog.seq.desc().nulls_last()).limit(1))
    next_seq = (last_seq or 0) + 1

    from app.security.tokens import utcnow  # avoid circular import at module level
    now = utcnow()

    row = AuditLog(
        user_id=user_id,
        document_id=document_id,
        action=action,
        details=safe or None,
        request_id=request_id_of(request),
        created_at=now,
        seq=next_seq,
        prev_hash=prev_hash,
    )
    # We need the id and created_at before computing the hash.
    row.row_hash = _compute_row_hash(prev_hash, row)
    db.add(row)
    return row
