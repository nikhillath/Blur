import hashlib
import logging
import uuid
from typing import BinaryIO

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.ratelimit import limiter
from app.db.session import get_db
from app.models import Document, DocumentReviewer, RedactedDocument, Scan, User
from app.schemas.auth import Page
from app.schemas.documents import DocumentDetail, DocumentListItem, DocumentOut, ScanOut
from app.schemas.review import ReviewerAssign, ReviewerOut
from app.security.deps import get_current_user
from app.services.access import get_owned_document
from app.services import scan_runner
from app.services.audit import log_event
from app.services.downloads import attachment
from app.services.file_validation import ValidationError, validate_upload
from app.storage.local import LocalStorage, StorageError, get_storage

logger = logging.getLogger("pii_sentinel.documents")
router = APIRouter(prefix="/documents", tags=["documents"])

ACTIVE_SCAN = ("PENDING", "PROCESSING")
DOC_STATUSES = {"UPLOADED", "PROCESSING", "SCANNED", "FAILED"}


def _read_limited(stream: BinaryIO, max_bytes: int) -> bytes:
    buf = bytearray()
    while chunk := stream.read(1024 * 1024):
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds the {max_bytes // (1024 * 1024)} MB limit",
            )
    return bytes(buf)


@router.post("", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(get_settings().heavy_rate_limit)
def upload_document(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    storage: LocalStorage = Depends(get_storage),
) -> Document:
    data = _read_limited(file.file, get_settings().max_upload_bytes)
    try:
        info = validate_upload(file.filename, file.content_type, data)
    except ValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    key = f"originals/{uuid.uuid4().hex}.{info.file_type}"  # server-generated; never from the filename
    try:
        storage.save(key, data)
    except StorageError:
        logger.exception("storage save failed")
        raise HTTPException(status_code=500, detail="could not store file")

    try:
        doc = Document(
            uploaded_by=user.id,
            original_filename=info.safe_name,
            storage_key=key,
            file_type=info.file_type,
            mime_type=info.mime_type,
            file_size=info.size,
            sha256=hashlib.sha256(data).hexdigest(),
            status="UPLOADED",
        )
        db.add(doc)
        db.flush()
        log_event(
            db, "DOCUMENT_UPLOADED", user_id=user.id, document_id=doc.id,
            details={"file_type": info.file_type, "size": info.size}, request=request,
        )
        db.commit()
    except Exception:
        db.rollback()
        try:
            storage.delete(key)  # no orphan file when the DB write failed
        except StorageError:
            logger.exception("failed to clean up orphaned file")
        raise
    return doc


def build_list_items(db: Session, docs: list[Document]) -> list[DocumentListItem]:
    scans_by_doc: dict[uuid.UUID, list[Scan]] = {d.id: [] for d in docs}
    if docs:
        for s in db.scalars(select(Scan).where(Scan.document_id.in_(scans_by_doc)).order_by(Scan.started_at)):
            scans_by_doc[s.document_id].append(s)
    items = []
    for d in docs:
        scans = scans_by_doc[d.id]
        items.append(DocumentListItem.model_validate({
            **DocumentOut.model_validate(d).model_dump(),
            "scan_count": len(scans),
            "latest_scan": ScanOut.model_validate(scans[-1]) if scans else None,
        }))
    return items


@router.get("", response_model=Page[DocumentListItem])
def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    q: str | None = Query(None, max_length=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Page[DocumentListItem]:
    if status_filter is not None and status_filter not in DOC_STATUSES:
        raise HTTPException(status_code=422, detail="invalid status filter")
    base = select(Document).where(Document.uploaded_by == user.id)
    if status_filter:
        base = base.where(Document.status == status_filter)
    if q:
        q = q.replace("\x00", "")
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        base = base.where(Document.original_filename.ilike(f"%{escaped}%", escape="\\"))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    docs = db.scalars(
        base.order_by(Document.uploaded_at.desc(), Document.id).offset((page - 1) * page_size).limit(page_size)
    ).all()

    items = build_list_items(db, docs)
    return Page(items=items, total=total, page=page, page_size=page_size)


@router.get("/{document_id}", response_model=DocumentDetail)
def get_document(
    document_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Document:
    return get_owned_document(db, user, document_id)


@router.get("/{document_id}/download")
def download_original(
    document_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    doc = get_owned_document(db, user, document_id)
    response = attachment(doc.storage_key, doc.mime_type, doc.original_filename)  # 404 if the file is gone
    log_event(db, "DOCUMENT_DOWNLOADED", user_id=user.id, document_id=doc.id, request=request)
    db.commit()
    return response


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    storage: LocalStorage = Depends(get_storage),
) -> None:
    """Deletes the document, its scans, entities and redacted outputs, then the stored files.
    Audit rows are kept (they hold no foreign key to the document)."""
    doc = get_owned_document(db, user, document_id, for_update=True)
    keys = [doc.storage_key] + list(
        db.scalars(select(RedactedDocument.storage_key).where(RedactedDocument.original_document_id == doc.id))
    )
    log_event(
        db, "DOCUMENT_DELETED", user_id=user.id, document_id=doc.id,
        details={"file_type": doc.file_type, "files_removed": len(keys)}, request=request,
    )
    db.delete(doc)
    db.commit()
    for key in keys:  # after commit: a failed file delete leaves an orphan blob, never a dangling record
        try:
            storage.delete(key)
        except StorageError:
            logger.exception("failed to remove stored file after document delete")


@router.post("/{document_id}/scan", response_model=ScanOut, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(get_settings().heavy_rate_limit)
def create_scan(
    document_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Scan:
    # Row lock serialises concurrent requests so only one active scan can exist per document.
    doc = get_owned_document(db, user, document_id, for_update=True)
    scan_runner.recover_stale_scans(db, doc.id)  # a long-dead scan must not block new ones
    active = db.scalar(
        select(func.count()).select_from(Scan).where(Scan.document_id == doc.id, Scan.status.in_(ACTIVE_SCAN))
    )
    if active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a scan is already in progress")
    scan = Scan(document_id=doc.id, model_version="unassigned", detection_mode="unassigned", status="PENDING")
    db.add(scan)
    db.flush()
    log_event(db, "SCAN_REQUESTED", user_id=user.id, document_id=doc.id, details={"scan_id": str(scan.id)}, request=request)
    db.commit()
    background.add_task(scan_runner.run_scan, scan.id)
    return scan


@router.get("/{document_id}/scans", response_model=list[ScanOut])
def list_scans(
    document_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[Scan]:
    return get_owned_document(db, user, document_id).scans


# ----------------------------------------------------------------------------- reviewer assignment
@router.post("/{document_id}/reviewers", response_model=ReviewerOut, status_code=status.HTTP_201_CREATED)
def assign_reviewer(
    document_id: uuid.UUID, body: ReviewerAssign, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> ReviewerOut:
    """Owner-only. Lets a REVIEWER/ADMIN-role user review (not delete or download) this document."""
    doc = get_owned_document(db, user, document_id)
    reviewer = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if reviewer is None or not reviewer.is_active or reviewer.role not in ("REVIEWER", "ADMIN"):
        raise HTTPException(status_code=422, detail="no active reviewer found with that email")
    if reviewer.id == user.id:
        raise HTTPException(status_code=422, detail="you already have access to your own document")
    if db.scalar(select(DocumentReviewer.id).where(DocumentReviewer.document_id == doc.id, DocumentReviewer.reviewer_id == reviewer.id)):
        raise HTTPException(status_code=409, detail="reviewer already assigned")
    row = DocumentReviewer(document_id=doc.id, reviewer_id=reviewer.id, assigned_by=user.id)
    db.add(row)
    db.flush()
    log_event(db, "REVIEWER_ASSIGNED", user_id=user.id, document_id=doc.id,
              details={"reviewer_id": str(reviewer.id)}, request=request)
    db.commit()
    return ReviewerOut(user_id=reviewer.id, name=reviewer.name, email=reviewer.email, assigned_at=row.created_at)


@router.get("/{document_id}/reviewers", response_model=list[ReviewerOut])
def list_reviewers(
    document_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[ReviewerOut]:
    doc = get_owned_document(db, user, document_id)
    rows = db.execute(select(DocumentReviewer, User).join(User, User.id == DocumentReviewer.reviewer_id)
                      .where(DocumentReviewer.document_id == doc.id).order_by(DocumentReviewer.created_at)).all()
    return [ReviewerOut(user_id=u.id, name=u.name, email=u.email, assigned_at=r.created_at) for r, u in rows]


@router.delete("/{document_id}/reviewers/{reviewer_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_reviewer(
    document_id: uuid.UUID, reviewer_id: uuid.UUID, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> None:
    doc = get_owned_document(db, user, document_id)
    row = db.scalar(select(DocumentReviewer).where(DocumentReviewer.document_id == doc.id, DocumentReviewer.reviewer_id == reviewer_id))
    if row is None:
        raise HTTPException(status_code=404, detail="reviewer not assigned")
    log_event(db, "REVIEWER_REMOVED", user_id=user.id, document_id=doc.id, details={"reviewer_id": str(reviewer_id)}, request=request)
    db.delete(row)
    db.commit()
