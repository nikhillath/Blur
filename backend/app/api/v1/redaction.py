import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.ratelimit import limiter
from app.db.session import get_db
from app.models import Document, RedactedDocument, User
from app.security.deps import get_current_user
from app.services.access import get_owned_document, get_scan_for_review, is_assigned_reviewer
from app.services.audit import log_event
from app.services.downloads import attachment
from app.services.redaction_service import MIME, redact_scan
from app.storage.local import StorageError, get_storage

router = APIRouter(tags=["redaction"])


class RedactRequest(BaseModel):
    mask_style: Literal["full", "partial"] = "full"


class RedactedOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_document_id: uuid.UUID
    source_scan_id: uuid.UUID
    file_type: str
    redaction_count: int
    validation_status: str
    validation_details: dict | None
    created_at: datetime


def _get_redacted(db: Session, user: User, rid: uuid.UUID) -> tuple[RedactedDocument, Document]:
    row = db.get(RedactedDocument, rid)
    doc = db.get(Document, row.original_document_id) if row else None
    if row is None or not (doc.uploaded_by == user.id or is_assigned_reviewer(db, user, doc.id)):
        raise HTTPException(status_code=404, detail="redacted document not found")
    return row, doc


@router.post("/scans/{scan_id}/redact", response_model=RedactedOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(get_settings().heavy_rate_limit)
def create_redaction(
    scan_id: uuid.UUID, request: Request, body: RedactRequest | None = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> RedactedDocument:
    """Builds a NEW file from the entities currently marked REDACT. The original is never modified."""
    scan = get_scan_for_review(db, user, scan_id)
    if scan.status != "COMPLETED":
        raise HTTPException(status_code=409, detail="the scan is not completed")
    doc = db.get(Document, scan.document_id)
    return redact_scan(db, scan, doc, user, (body or RedactRequest()).mask_style, request)


@router.get("/documents/{document_id}/redacted", response_model=list[RedactedOut])
def list_redacted(document_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = db.get(Document, document_id)
    if doc is None or not (doc.uploaded_by == user.id or is_assigned_reviewer(db, user, doc.id)):
        raise HTTPException(status_code=404, detail="document not found")
    return db.scalars(select(RedactedDocument).where(RedactedDocument.original_document_id == doc.id)
                      .order_by(RedactedDocument.created_at.desc())).all()


@router.get("/redacted-documents/{redacted_id}", response_model=RedactedOut)
def get_redacted(redacted_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _get_redacted(db, user, redacted_id)[0]


@router.get("/redacted-documents/{redacted_id}/download")
def download_redacted(
    redacted_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    row, doc = _get_redacted(db, user, redacted_id)
    if row.validation_status == "FAILED":
        raise HTTPException(status_code=409, detail="this redaction failed verification and was not saved")
    response = attachment(row.storage_key, MIME[row.file_type], f"redacted_{doc.original_filename}")
    log_event(db, "REDACTED_DOWNLOADED", user_id=user.id, document_id=doc.id, details={"redacted_id": str(row.id)}, request=request)
    db.commit()
    return response


@router.delete("/redacted-documents/{redacted_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_redacted(redacted_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    row, doc = _get_redacted(db, user, redacted_id)
    get_owned_document(db, user, doc.id)  # only the owner may delete outputs
    key = row.storage_key
    log_event(db, "REDACTED_DELETED", user_id=user.id, document_id=doc.id, details={"redacted_id": str(row.id)}, request=request)
    db.delete(row)
    db.commit()
    try:
        get_storage().delete(key)
    except StorageError:
        pass
