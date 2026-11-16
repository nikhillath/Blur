import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.ml.taxonomy import ENTITY_SET
from app.models import PiiEntity, Scan, User
from app.schemas.documents import EntityPage, ExtractedTextOut, ScanOut
from app.security.deps import get_current_user
from app.services import scan_runner
from app.services.access import get_owned_document, get_owned_scan, get_scan_for_review
from app.services.audit import log_event

router = APIRouter(prefix="/scans", tags=["scans"])


@router.get("/{scan_id}", response_model=ScanOut)
def get_scan(scan_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Scan:
    return get_scan_for_review(db, user, scan_id)


@router.get("/{scan_id}/text", response_model=ExtractedTextOut)
def get_extracted_text(
    scan_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExtractedTextOut:
    scan = get_scan_for_review(db, user, scan_id)
    if scan.status != "COMPLETED" or scan.extracted_text is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="text is not available for this scan")
    log_event(db, "EXTRACTED_TEXT_VIEWED", user_id=user.id, document_id=scan.document_id,
              details={"scan_id": str(scan.id)}, request=request)
    db.commit()
    page_map = scan.page_map or {}
    return ExtractedTextOut(
        scan_id=scan.id, text=scan.extracted_text, coord_space=page_map.get("coord_space"),
        pages=page_map.get("pages", []), quality=scan.extraction_info,
    )


@router.get("/{scan_id}/entities", response_model=EntityPage)
def list_entities(
    scan_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    entity_type: str | None = None,
    review_status: str | None = Query(None, pattern="^(PENDING|REDACT|KEEP|FALSE_POSITIVE)$"),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EntityPage:
    scan = get_scan_for_review(db, user, scan_id)
    if entity_type is not None and entity_type not in ENTITY_SET:
        raise HTTPException(status_code=422, detail="unknown entity type")
    q = select(PiiEntity).where(PiiEntity.scan_id == scan.id, PiiEntity.confidence_score >= min_confidence)
    if entity_type:
        q = q.where(PiiEntity.entity_type == entity_type)
    if review_status:
        q = q.where(PiiEntity.review_status == review_status)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(
        q.order_by(PiiEntity.start_position, PiiEntity.id).offset((page - 1) * page_size).limit(page_size)
    ).all()
    counts = dict(
        db.execute(
            select(PiiEntity.entity_type, func.count()).where(PiiEntity.scan_id == scan.id).group_by(PiiEntity.entity_type)
        ).all()
    )
    by_status = dict(
        db.execute(
            select(PiiEntity.review_status, func.count()).where(PiiEntity.scan_id == scan.id).group_by(PiiEntity.review_status)
        ).all()
    )
    return EntityPage(items=rows, total=total, page=page, page_size=page_size, counts_by_type=counts, counts_by_status=by_status)


@router.post("/{scan_id}/retry", response_model=ScanOut, status_code=status.HTTP_202_ACCEPTED)
def retry_scan(
    scan_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Scan:
    """Retries a FAILED scan as a new scan; the failed one stays in the history."""
    failed = get_owned_scan(db, user, scan_id)
    if failed.status != "FAILED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="only failed scans can be retried")
    doc = get_owned_document(db, user, failed.document_id, for_update=True)
    scan_runner.recover_stale_scans(db, doc.id)
    active = db.scalar(
        select(func.count()).select_from(Scan).where(
            Scan.document_id == doc.id, Scan.status.in_(("PENDING", "PROCESSING"))
        )
    )
    if active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a scan is already in progress")
    scan = Scan(document_id=doc.id, model_version="unassigned", detection_mode="unassigned", status="PENDING")
    db.add(scan)
    db.flush()
    log_event(db, "SCAN_RETRIED", user_id=user.id, document_id=doc.id,
              details={"scan_id": str(scan.id), "retry_of": str(failed.id)}, request=request)
    db.commit()
    background.add_task(scan_runner.run_scan, scan.id)
    return scan
