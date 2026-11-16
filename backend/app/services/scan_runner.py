import logging
import time
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.document_processing.extractors import extract
from app.document_processing.types import Deadline, ExtractionError
from app.ml.pipeline import run_detection
from app.models import Document, PiiEntity, Scan
from app.security.tokens import utcnow
from app.services.audit import log_event
from app.storage.local import get_storage

logger = logging.getLogger("pii_sentinel.scan")


def _settle_document_status(db: Session, doc: Document) -> None:
    """A document is SCANNED if any scan ever completed; a later failure must not hide that."""
    done = db.scalar(
        select(Scan.id).where(Scan.document_id == doc.id, Scan.status == "COMPLETED").limit(1)
    )
    doc.status = "SCANNED" if done else "FAILED"


def _fail(db: Session, scan_id: uuid.UUID, message: str) -> None:
    db.rollback()
    scan = db.get(Scan, scan_id)
    if scan is None:
        return
    scan.status = "FAILED"
    scan.error_message = message[:500]
    scan.completed_at = utcnow()
    doc = db.get(Document, scan.document_id)
    if doc is not None:
        _settle_document_status(db, doc)
    log_event(db, "SCAN_FAILED", user_id=doc.uploaded_by if doc else None,
              document_id=scan.document_id, details={"scan_id": str(scan.id), "error": message[:200]})
    db.commit()


def run_scan(scan_id: uuid.UUID) -> None:
    """Runs one scan to a terminal state (COMPLETED or FAILED); never leaves it PROCESSING.

    Pipeline: extract text -> detect PII -> persist scan + entities in one transaction, so a scan
    is never COMPLETED without its entities.
    """
    with SessionLocal() as db:
        # Atomic claim: if several workers/tasks pick up the same scan, exactly one wins.
        claimed = db.execute(
            update(Scan).where(Scan.id == scan_id, Scan.status == "PENDING").values(status="PROCESSING")
        ).rowcount
        if not claimed:
            db.rollback()
            return
        scan = db.get(Scan, scan_id)
        doc = db.get(Document, scan.document_id)
        doc.status = "PROCESSING"
        db.commit()
        started = time.monotonic()
        try:
            data = get_storage().read(doc.storage_key)
            result = extract(doc.file_type, data, Deadline(get_settings().scan_timeout_seconds))
            scan.extracted_text = result.text
            scan.page_map = result.page_map()
            scan.extraction_info = result.quality
            deadline = Deadline(get_settings().scan_timeout_seconds)
            out = run_detection(result.text, scan.page_map, deadline)
            scan.model_version = out.version
            scan.detection_mode = out.mode
            scan.detector_config = out.config
            for e in out.entities:
                d = e.detection
                db.add(PiiEntity(
                    scan_id=scan.id, entity_type=d.entity_type, original_entity_type=d.entity_type,
                    detected_value=e.value, masked_value=e.masked, confidence_score=d.score,
                    start_position=d.start, end_position=d.end, page_number=e.page, bbox=e.bbox,
                    detection_method=d.method, model_version=out.version, review_status="PENDING",
                    provenance=d.provenance,
                ))
            scan.status = "COMPLETED"
            scan.completed_at = utcnow()
            scan.duration_ms = int((time.monotonic() - started) * 1000)
            doc.status = "SCANNED"
            doc.processed_at = scan.completed_at
            log_event(db, "SCAN_COMPLETED", user_id=doc.uploaded_by, document_id=doc.id,
                      details={"scan_id": str(scan.id), "chars": len(result.text), "entities": len(out.entities),
                               "mode": out.mode, "duration_ms": scan.duration_ms})
            db.commit()
        except ExtractionError as exc:
            _fail(db, scan_id, exc.message)
        except Exception:
            logger.exception("scan %s failed unexpectedly", scan_id)
            _fail(db, scan_id, "processing failed")


def stale_cutoff():
    # Anything active for longer than the processing limit (plus slack) cannot still be running.
    return utcnow() - timedelta(seconds=get_settings().scan_timeout_seconds + 60)


def recover_stale_scans(db: Session | None = None, document_id: uuid.UUID | None = None, *, startup: bool = False) -> int:
    """Fail scans stuck PENDING/PROCESSING. Only scans older than the processing limit are touched,
    so a second worker's in-flight scan is never failed. ``startup=True`` treats every active scan as
    stale (single-process dev: nothing can survive a restart)."""
    own = db is None
    db = db or SessionLocal()
    try:
        q = select(Scan).where(Scan.status.in_(("PENDING", "PROCESSING")))
        if document_id is not None:
            q = q.where(Scan.document_id == document_id)
        if not startup:
            q = q.where(Scan.started_at < stale_cutoff())
        stuck = db.scalars(q).all()
        for scan in stuck:
            scan.status = "FAILED"
            scan.error_message = "interrupted by a server restart" if startup else "scan timed out"
            scan.completed_at = utcnow()
            doc = db.get(Document, scan.document_id)
            if doc is not None:
                _settle_document_status(db, doc)
        if own:
            db.commit()
        else:
            db.flush()  # the caller owns the transaction (and any row lock it holds)
        return len(stuck)
    finally:
        if own:
            db.close()

