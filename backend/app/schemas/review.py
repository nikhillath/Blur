import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Action = Literal["REDACT", "KEEP", "FALSE_POSITIVE"]


class ReviewRequest(BaseModel):
    action: Action
    reason: str | None = Field(None, max_length=500)


class EntityPatch(BaseModel):
    entity_type: str = Field(min_length=1, max_length=32)


class BulkReviewRequest(BaseModel):
    """Select either explicit ``entity_ids`` or by filter. Filters only touch PENDING entities by
    default, so a bulk action cannot silently override earlier human decisions."""

    action: Action
    reason: str | None = Field(None, max_length=500)
    entity_ids: list[uuid.UUID] | None = Field(None, max_length=2000)
    entity_type: str | None = None
    min_confidence: float | None = Field(None, ge=0, le=1)
    max_confidence: float | None = Field(None, ge=0, le=1)
    only_pending: bool = True

    @model_validator(mode="after")
    def need_a_selector(self) -> "BulkReviewRequest":
        if not self.entity_ids and self.entity_type is None and self.min_confidence is None and self.max_confidence is None:
            raise ValueError("select entities with entity_ids or at least one filter")
        if self.entity_ids and (self.entity_type is not None or self.min_confidence is not None or self.max_confidence is not None):
            raise ValueError("use either entity_ids or filters, not both")
        return self


class BulkReviewResult(BaseModel):
    updated: int
    skipped: int  # selected ids that are not in this scan (or not pending, when only_pending applies)


class ManualEntityCreate(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    entity_type: str = Field(min_length=1, max_length=32)
    reason: str | None = Field(None, max_length=500)


class ReviewHistoryItem(BaseModel):
    id: uuid.UUID
    action: str
    reason: str | None
    decided_by: uuid.UUID
    decided_by_name: str
    decided_at: datetime


class ReviewerAssign(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ReviewerOut(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str
    assigned_at: datetime
