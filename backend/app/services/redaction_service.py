import io
import uuid

import pymupdf
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.document_processing.extractors import extract_docx
from app.document_processing.ocr import get_ocr_engine, recognize_with_deadline
from app.document_processing.types import Deadline, ExtractionError
from app.models import Document, PiiEntity, RedactedDocument, Scan, User
from app.redaction import docx as docx_red, image as image_red, pdf as pdf_red
from app.redaction.plan import RedactionError, check_absent, merge_targets
from app.redaction.txt import redact_text
from app.services.audit import log_event
from app.storage.local import StorageError, get_storage

MIME = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8", "png": "image/png", "jpg": "image/jpeg",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def _image_text(data: bytes, deadline: Deadline) -> str | None:
    """OCR the redacted image to prove the text is gone. None = OCR unavailable (cannot verify)."""
    from PIL import Image

    try:
        lines = recognize_with_deadline(get_ocr_engine(), Image.open(io.BytesIO(data)), deadline)
    except ExtractionError:
        return None
    return "\n".join(ln.text for ln in lines)


def redact_scan(db: Session, scan: Scan, doc: Document, user: User, style: str, request=None) -> RedactedDocument:
    entities = list(db.scalars(select(PiiEntity).where(PiiEntity.scan_id == scan.id, PiiEntity.review_status == "REDACT")
                               .order_by(PiiEntity.start_position)))
    if not entities:
        raise HTTPException(status_code=409, detail="no entities are marked REDACT")
    pending = db.scalar(select(func.count()).select_from(PiiEntity)
                        .where(PiiEntity.scan_id == scan.id, PiiEntity.review_status == "PENDING")) or 0
    storage, text, page_map = get_storage(), scan.extracted_text or "", scan.page_map or {}
    targets, deadline = merge_targets(entities), Deadline(get_settings().scan_timeout_seconds)
    try:
        original = storage.read(doc.storage_key)
        ft = doc.file_type
        details: dict = {"mask_style_requested": style, "mask_style_applied": style if ft in ("txt", "docx") else "full"}
        checks: dict = {}
        if ft == "txt":
            out_text = redact_text(text, targets, style)
            out, verify_text = out_text.encode("utf-8"), out_text
        elif ft == "pdf":
            out, info = pdf_red.redact_pdf(original, page_map, targets, text)
            methods = {p["page"]: p["method"] for p in page_map.get("pages", [])}
            verify_text = pdf_red.output_text(out, methods, deadline)
            checks["metadata_clean"] = pdf_red.metadata_clean(out)
            checks["page_count_preserved"] = pymupdf.open(stream=out, filetype="pdf").page_count == pymupdf.open(stream=original, filetype="pdf").page_count
            details.update(info)
        elif ft == "docx":
            out, info = docx_red.redact_docx(original, page_map, targets, text, style)
            verify_text = extract_docx(out, deadline).text + "\n" + docx_red.all_parts_text(out)
            details.update(info)
        elif ft in ("png", "jpg"):
            out, info = image_red.redact_image(original, page_map, targets, ft)
            verify_text = _image_text(out, deadline)
            details.update(info)
        else:
            raise RedactionError("this file type cannot be redacted")
    except RedactionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except (ExtractionError, StorageError) as exc:
        raise HTTPException(status_code=422, detail=getattr(exc, "message", "redaction failed"))

    if verify_text is None:
        status, leak = "SKIPPED", {"note": "OCR unavailable: output could not be verified"}
    else:
        leak = check_absent(text, verify_text, [e.detected_value for e in entities])
        status = "PASSED" if leak["passed"] and all(checks.values()) else "FAILED"
    details.update({"verification": leak, "checks": checks, "pending_entities_at_redaction": pending})

    key = f"redacted/{uuid.uuid4().hex}.{doc.file_type}"
    if status != "FAILED":  # a file that failed verification is never kept
        storage.save(key, out)
    row = RedactedDocument(
        original_document_id=doc.id, source_scan_id=scan.id, created_by=user.id, storage_key=key,
        file_type=doc.file_type, redaction_count=len(entities), validation_status=status, validation_details=details,
    )
    db.add(row)
    db.flush()
    log_event(db, "REDACTION_CREATED" if status != "FAILED" else "REDACTION_FAILED_VALIDATION", user_id=user.id,
              document_id=doc.id, request=request,
              details={"redacted_id": str(row.id), "scan_id": str(scan.id), "count": len(entities), "validation": status})
    db.commit()
    return row
