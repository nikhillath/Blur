"""Retention and purge service (gap 2).

Deletes documents older than RETENTION_DAYS and audit rows older than AUDIT_RETENTION_DAYS.
Runs from the CLI (python -m scripts.purge) and can be scheduled in the app lifespan.

Purge order:
  1. Delete expired Documents (cascades scans, entities, redacted docs, reviewer rows).
  2. Remove their stored files.
  3. Delete expired AuditLog rows.
  4. Write a single RETENTION_PURGE audit event with counts (it is not itself subject to retention).
"""
import logging
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import AuditLog, Document, RedactedDocument
from app.security.tokens import utcnow
from app.services.audit import log_event
from app.storage.local import StorageError, get_storage

logger = logging.getLogger("pii_sentinel.retention")


def purge_expired(db: Session, *, dry_run: bool = False) -> dict:
    """Delete documents and audit rows that have exceeded the configured retention windows.

    Returns a dict with counts of what was (or would be) deleted.
    With dry_run=True nothing is committed.
    """
    s = get_settings()
    now = utcnow()
    counts: dict = {"documents": 0, "audit_rows": 0, "files_deleted": 0, "files_failed": 0}

    # ── 1. Expired documents ───────────────────────────────────────────────────
    if s.retention_days > 0:
        cutoff = now - timedelta(days=s.retention_days)
        expired_docs = db.scalars(
            select(Document).where(Document.uploaded_at < cutoff)
        ).all()
        for doc in expired_docs:
            # Collect file keys before cascade delete removes the rows.
            keys = [doc.storage_key] + list(
                db.scalars(
                    select(RedactedDocument.storage_key).where(
                        RedactedDocument.original_document_id == doc.id
                    )
                )
            )
            counts["documents"] += 1
            if not dry_run:
                db.delete(doc)
                db.flush()
                for key in keys:
                    try:
                        get_storage().delete(key)
                        counts["files_deleted"] += 1
                    except StorageError:
                        logger.warning("retention: failed to delete file %s", key)
                        counts["files_failed"] += 1
        logger.info(
            "retention: %s%d document(s) older than %d days",
            "[dry-run] would delete " if dry_run else "deleted ",
            counts["documents"],
            s.retention_days,
        )

    # ── 2. Expired audit rows ─────────────────────────────────────────────────
    if s.audit_retention_days > 0:
        audit_cutoff = now - timedelta(days=s.audit_retention_days)
        if dry_run:
            counts["audit_rows"] = db.scalar(
                select(AuditLog).where(AuditLog.created_at < audit_cutoff).with_only_columns(
                    AuditLog.id  # type: ignore[arg-type]
                )
            ) or 0
        else:
            result = db.execute(delete(AuditLog).where(AuditLog.created_at < audit_cutoff))
            counts["audit_rows"] = result.rowcount
        logger.info(
            "retention: %s%d audit row(s) older than %d days",
            "[dry-run] would delete " if dry_run else "deleted ",
            counts["audit_rows"],
            s.audit_retention_days,
        )

    # ── 3. Audit the purge itself ─────────────────────────────────────────────
    if not dry_run and (counts["documents"] or counts["audit_rows"]):
        log_event(db, "RETENTION_PURGE", details={
            "documents_deleted": counts["documents"],
            "audit_rows_deleted": counts["audit_rows"],
            "files_deleted": counts["files_deleted"],
            "files_failed": counts["files_failed"],
        })
        db.commit()

    return counts
