from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import AuditLog, Document, RedactedDocument, Scan, User
from app.security.deps import get_current_user

router = APIRouter(tags=["dashboard"])

_PENDING = text("""
    SELECT count(e.id), count(DISTINCT s.document_id)
    FROM (SELECT DISTINCT ON (document_id) id, document_id FROM scans
          WHERE status = 'COMPLETED' ORDER BY document_id, started_at DESC) s
    JOIN documents d ON d.id = s.document_id
    JOIN pii_entities e ON e.scan_id = s.id AND e.review_status = 'PENDING'
    WHERE d.uploaded_by = :uid
""")


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """Numbers for the signed-in user's own documents, computed from the database."""
    by_status = dict(db.execute(select(Document.status, func.count()).where(Document.uploaded_by == user.id)
                                .group_by(Document.status)).all())
    pending_entities, awaiting = db.execute(_PENDING, {"uid": user.id}).one()
    failed_scans = db.scalar(select(func.count()).select_from(Scan).join(Document, Document.id == Scan.document_id)
                             .where(Document.uploaded_by == user.id, Scan.status == "FAILED")) or 0
    redacted = db.scalar(select(func.count()).select_from(RedactedDocument).join(Document, Document.id == RedactedDocument.original_document_id)
                         .where(Document.uploaded_by == user.id, RedactedDocument.validation_status != "FAILED")) or 0
    activity = db.execute(select(AuditLog.action, AuditLog.created_at, AuditLog.document_id)
                          .where(AuditLog.user_id == user.id).order_by(AuditLog.created_at.desc()).limit(10)).all()
    return {
        "documents": sum(by_status.values()), "by_status": by_status, "failed_scans": failed_scans,
        "redacted_documents": redacted, "pending_entities": pending_entities, "awaiting_review_documents": awaiting,
        "recent_activity": [{"action": a, "created_at": c, "document_id": d} for a, c, d in activity],
    }
