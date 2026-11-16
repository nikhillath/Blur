import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.document_processing.locate import locate
from app.ml.taxonomy import ENTITY_SET, mask_value
from app.models import Document, DocumentReviewer, PiiEntity, RedactionAction, Scan, User
from app.schemas.auth import Page
from app.schemas.documents import DocumentListItem, EntityOut
from app.schemas.review import (
    BulkReviewRequest, BulkReviewResult, EntityPatch, ManualEntityCreate, ReviewHistoryItem, ReviewRequest,
)
from app.security.deps import get_current_user
from app.services.access import get_entity_for_review, get_scan_for_review
from app.services.audit import log_event

router = APIRouter(tags=["review"])

STATUS_FOR_ACTION = {"REDACT": "REDACT", "KEEP": "KEEP", "FALSE_POSITIVE": "FALSE_POSITIVE"}
MAX_MANUAL_SPAN = 500


def _check_type(entity_type: str) -> None:
    if entity_type not in ENTITY_SET:
        raise HTTPException(status_code=422, detail="unknown entity type")


def record_decision(db: Session, entity: PiiEntity, user: User, action: str, reason: str | None) -> None:
    """Append to the immutable decision history and update the entity's current status."""
    # clock_timestamp() is taken when the INSERT runs, i.e. while this transaction holds the entity row lock,
    # so history order always matches decision order. now() would be the transaction START time, which can
    # disagree with lock order under concurrency.
    db.add(RedactionAction(entity_id=entity.id, decided_by=user.id, action=action, reason=reason,
                           decided_at=func.clock_timestamp()))
    entity.review_status = STATUS_FOR_ACTION[action]


def _require_completed(scan: Scan) -> None:
    if scan.status != "COMPLETED":
        raise HTTPException(status_code=409, detail="the scan is not completed")


@router.post("/entities/{entity_id}/review", response_model=EntityOut)
def review_entity(
    entity_id: uuid.UUID, body: ReviewRequest, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> PiiEntity:
    entity, scan = get_entity_for_review(db, user, entity_id, for_update=True)
    previous = entity.review_status
    record_decision(db, entity, user, body.action, body.reason)
    log_event(db, "ENTITY_REVIEWED", user_id=user.id, document_id=scan.document_id, request=request,
              details={"entity_id": str(entity.id), "from": previous, "to": entity.review_status, "type": entity.entity_type})
    db.commit()
    return entity


@router.patch("/entities/{entity_id}", response_model=EntityOut)
def correct_entity_type(
    entity_id: uuid.UUID, body: EntityPatch, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> PiiEntity:
    """Reclassify an entity. The model's original type is kept in ``original_entity_type``."""
    _check_type(body.entity_type)
    entity, scan = get_entity_for_review(db, user, entity_id, for_update=True)
    if body.entity_type != entity.entity_type:
        before = entity.entity_type
        entity.entity_type = body.entity_type
        entity.masked_value = mask_value(body.entity_type, entity.detected_value)
        entity.provenance = [*(entity.provenance or []), {"correction": {"from": before, "to": body.entity_type, "by": str(user.id)}}]
        log_event(db, "ENTITY_TYPE_CORRECTED", user_id=user.id, document_id=scan.document_id, request=request,
                  details={"entity_id": str(entity.id), "from": before, "to": body.entity_type})
        db.commit()
    return entity


@router.get("/entities/{entity_id}/history", response_model=list[ReviewHistoryItem])
def entity_history(
    entity_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[ReviewHistoryItem]:
    entity, _ = get_entity_for_review(db, user, entity_id)
    rows = db.execute(
        select(RedactionAction, User.name).join(User, User.id == RedactionAction.decided_by)
        .where(RedactionAction.entity_id == entity.id).order_by(RedactionAction.decided_at, RedactionAction.id)
    ).all()
    return [ReviewHistoryItem(id=a.id, action=a.action, reason=a.reason, decided_by=a.decided_by,
                              decided_by_name=name, decided_at=a.decided_at) for a, name in rows]


@router.delete("/entities/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_manual_entity(
    entity_id: uuid.UUID, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> None:
    """Only reviewer-added spans can be removed; a model detection is dismissed with FALSE_POSITIVE."""
    entity, scan = get_entity_for_review(db, user, entity_id, for_update=True)
    if not entity.is_manual:
        raise HTTPException(status_code=409, detail="only manually added entities can be deleted; mark it as a false positive")
    log_event(db, "ENTITY_MANUAL_DELETED", user_id=user.id, document_id=scan.document_id, request=request,
              details={"entity_id": str(entity.id), "type": entity.entity_type})
    db.delete(entity)
    db.commit()


@router.post("/scans/{scan_id}/reviews/bulk", response_model=BulkReviewResult)
def bulk_review(
    scan_id: uuid.UUID, body: BulkReviewRequest, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> BulkReviewResult:
    scan = get_scan_for_review(db, user, scan_id)
    _require_completed(scan)
    q = select(PiiEntity).where(PiiEntity.scan_id == scan.id)
    if body.entity_ids:
        q = q.where(PiiEntity.id.in_(set(body.entity_ids)))
    else:
        if body.entity_type is not None:
            _check_type(body.entity_type)
            q = q.where(PiiEntity.entity_type == body.entity_type)
        if body.min_confidence is not None:
            q = q.where(PiiEntity.confidence_score >= body.min_confidence)
        if body.max_confidence is not None:
            q = q.where(PiiEntity.confidence_score <= body.max_confidence)
        if body.only_pending:
            q = q.where(PiiEntity.review_status == "PENDING")
    entities = db.scalars(q.order_by(PiiEntity.id).with_for_update()).all()  # stable lock order: no deadlocks
    for e in entities:
        record_decision(db, e, user, body.action, body.reason)
    requested = len(set(body.entity_ids)) if body.entity_ids else len(entities)
    log_event(db, "ENTITIES_BULK_REVIEWED", user_id=user.id, document_id=scan.document_id, request=request,
              details={"scan_id": str(scan.id), "action": body.action, "updated": len(entities)})
    db.commit()
    return BulkReviewResult(updated=len(entities), skipped=requested - len(entities))


@router.post("/scans/{scan_id}/entities", response_model=EntityOut, status_code=status.HTTP_201_CREATED)
def add_manual_entity(
    scan_id: uuid.UUID, body: ManualEntityCreate, request: Request,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> PiiEntity:
    """A reviewer marks text the detectors missed. Offsets index the canonical extracted text."""
    _check_type(body.entity_type)
    scan = get_scan_for_review(db, user, scan_id)
    _require_completed(scan)
    text = scan.extracted_text or ""
    if not (0 <= body.start < body.end <= len(text)):
        raise HTTPException(status_code=422, detail="span is outside the extracted text")
    value = text[body.start : body.end]
    if not value.strip() or value != value.strip():
        raise HTTPException(status_code=422, detail="span must be non-blank and not start or end with whitespace")
    if len(value) > MAX_MANUAL_SPAN:
        raise HTTPException(status_code=422, detail=f"span longer than {MAX_MANUAL_SPAN} characters")
    duplicate = db.scalar(select(func.count()).select_from(PiiEntity).where(
        PiiEntity.scan_id == scan.id, PiiEntity.start_position == body.start,
        PiiEntity.end_position == body.end, PiiEntity.entity_type == body.entity_type))
    if duplicate:
        raise HTTPException(status_code=409, detail="an entity with this span and type already exists")
    page, rects, _ = locate(scan.page_map, body.start, body.end)
    entity = PiiEntity(
        scan_id=scan.id, entity_type=body.entity_type, original_entity_type=body.entity_type,
        detected_value=value, masked_value=mask_value(body.entity_type, value), confidence_score=1.0,
        start_position=body.start, end_position=body.end, page_number=page, bbox=rects,
        detection_method="manual", model_version="manual", is_manual=True,
        provenance=[{"manual": {"by": str(user.id)}}],
    )
    db.add(entity)
    db.flush()
    record_decision(db, entity, user, "REDACT", body.reason or "added manually by reviewer")
    log_event(db, "ENTITY_MANUAL_ADDED", user_id=user.id, document_id=scan.document_id, request=request,
              details={"entity_id": str(entity.id), "type": body.entity_type, "start": body.start, "end": body.end})
    db.commit()
    return entity


@router.get("/review/assigned", response_model=Page[DocumentListItem])
def assigned_documents(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> Page[DocumentListItem]:
    """Documents other users have assigned to the caller for review (reviewer-capable roles only)."""
    from app.api.v1.documents import build_list_items

    if user.role not in ("REVIEWER", "ADMIN"):
        return Page(items=[], total=0, page=page, page_size=page_size)
    base = select(Document).join(DocumentReviewer, DocumentReviewer.document_id == Document.id).where(
        DocumentReviewer.reviewer_id == user.id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    docs = db.scalars(base.order_by(Document.uploaded_at.desc(), Document.id).offset((page - 1) * page_size).limit(page_size)).all()
    return Page(items=build_list_items(db, docs), total=total, page=page, page_size=page_size)
