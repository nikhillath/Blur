import uuid

from fastapi import HTTPException, status
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models import Document, DocumentReviewer, PiiEntity, Scan, User

REVIEWER_ROLES = ("REVIEWER", "ADMIN")


def _not_found(what: str) -> HTTPException:
    # 404 (not 403) so IDs of other users' resources are not confirmed to exist.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def get_owned_document(
    db: Session, user: User, document_id: uuid.UUID, *, for_update: bool = False
) -> Document:
    q = db.query(Document).filter(Document.id == document_id, Document.uploaded_by == user.id)
    if for_update:
        q = q.with_for_update()
    doc = q.one_or_none()
    if doc is None:
        raise _not_found("document")
    return doc


def get_owned_scan(db: Session, user: User, scan_id: uuid.UUID) -> Scan:
    scan = (
        db.query(Scan)
        .join(Document, Document.id == Scan.document_id)
        .filter(Scan.id == scan_id, Document.uploaded_by == user.id)
        .one_or_none()
    )
    if scan is None:
        raise _not_found("scan")
    return scan


def is_assigned_reviewer(db: Session, user: User, document_id: uuid.UUID) -> bool:
    """Assignment alone is not enough: the user must also still hold a reviewer-capable role."""
    if user.role not in REVIEWER_ROLES:
        return False
    return bool(db.scalar(select(exists().where(
        DocumentReviewer.document_id == document_id, DocumentReviewer.reviewer_id == user.id))))


def get_scan_for_review(db: Session, user: User, scan_id: uuid.UUID) -> Scan:
    """Scan visible to its document's owner or to a reviewer the owner assigned. Else 404."""
    row = db.execute(
        select(Scan, Document.uploaded_by).join(Document, Document.id == Scan.document_id).where(Scan.id == scan_id)
    ).first()
    if row is None or not (row[1] == user.id or is_assigned_reviewer(db, user, row[0].document_id)):
        raise _not_found("scan")
    return row[0]


def get_entity_for_review(
    db: Session, user: User, entity_id: uuid.UUID, *, for_update: bool = False
) -> tuple[PiiEntity, Scan]:
    q = (
        select(PiiEntity, Scan, Document.uploaded_by)
        .join(Scan, Scan.id == PiiEntity.scan_id)
        .join(Document, Document.id == Scan.document_id)
        .where(PiiEntity.id == entity_id)
    )
    if for_update:
        q = q.with_for_update(of=PiiEntity)  # serialise concurrent decisions on one entity
    row = db.execute(q).first()
    if row is None or not (row[2] == user.id or is_assigned_reviewer(db, user, row[1].document_id)):
        raise _not_found("entity")
    return row[0], row[1]
