import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ScanOut(BaseModel):
    """Public scan view. Extracted text and internal config are intentionally excluded."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    model_version: str
    detection_mode: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    duration_ms: int | None
    error_message: str | None
    extraction_info: dict | None = None
    detector_config: dict | None = None
    entity_count: int = 0


class DocumentOut(BaseModel):
    """Public document view. The internal storage key is never exposed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    file_type: str
    mime_type: str
    file_size: int
    sha256: str | None
    status: str
    uploaded_at: datetime
    processed_at: datetime | None


class DocumentListItem(DocumentOut):
    scan_count: int
    latest_scan: ScanOut | None


class DocumentDetail(DocumentOut):
    scans: list[ScanOut]


class ExtractedTextOut(BaseModel):
    """Canonical extracted text. All entity offsets index into ``text``."""

    scan_id: uuid.UUID
    text: str
    coord_space: str | None
    pages: list[dict]
    quality: dict | None


class EntityOut(BaseModel):
    """Detected entity. The full value is deliberately absent: show ``masked_value`` and, where a
    reviewer needs context, use the owner-only extracted text with ``start``/``end``."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scan_id: uuid.UUID
    entity_type: str
    original_entity_type: str
    masked_value: str
    confidence_score: float
    start_position: int
    end_position: int
    page_number: int | None
    bbox: list | None
    detection_method: str
    model_version: str
    review_status: str
    is_manual: bool
    provenance: list | None


class EntityPage(BaseModel):
    items: list[EntityOut]
    total: int
    page: int
    page_size: int
    counts_by_type: dict[str, int]
    counts_by_status: dict[str, int] = {}
