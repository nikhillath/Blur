import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import select
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship

from app.db.session import Base
from app.security.crypto import EncryptedText

ROLES = ("USER", "REVIEWER", "ADMIN")
DOC_STATUSES = ("UPLOADED", "PROCESSING", "SCANNED", "FAILED")
SCAN_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED")
REVIEW_STATUSES = ("PENDING", "REDACT", "KEEP", "FALSE_POSITIVE")
ACTIONS = ("REDACT", "KEEP", "FALSE_POSITIVE")
VALIDATION_STATUSES = ("PASSED", "FAILED", "SKIPPED")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="ck_users_role"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="USER")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Account lockout (gap 4b)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # One login = one family. Rotation keeps the family, so access tokens (which carry it) survive a refresh.
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(_in("status", DOC_STATUSES), name="ck_documents_status"),
        CheckConstraint("file_size >= 0", name="ck_documents_size"),
        Index("ix_documents_owner_uploaded", "uploaded_by", "uploaded_at"),
        Index("ix_documents_owner_sha256", "uploaded_by", "sha256"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="UPLOADED")
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    scans: Mapped[list["Scan"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="Scan.started_at"
    )


class Scan(Base):
    __tablename__ = "scans"
    __table_args__ = (
        CheckConstraint(_in("status", SCAN_STATUSES), name="ck_scans_status"),
        Index("ix_scans_document_started", "document_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    detector_config: Mapped[dict | None] = mapped_column(JSON)
    detection_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="rules-only")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    extracted_text: Mapped[str | None] = mapped_column(EncryptedText)
    page_map: Mapped[dict | None] = mapped_column(JSON)
    extraction_info: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(String(500))

    document: Mapped[Document] = relationship(back_populates="scans")
    entities: Mapped[list["PiiEntity"]] = relationship(
        back_populates="scan", cascade="all, delete-orphan", order_by="PiiEntity.start_position"
    )


class PiiEntity(Base):
    __tablename__ = "pii_entities"
    __table_args__ = (
        CheckConstraint("start_position >= 0 AND end_position > start_position", name="ck_entity_span"),
        CheckConstraint("confidence_score >= 0 AND confidence_score <= 1", name="ck_entity_conf"),
        CheckConstraint(_in("review_status", REVIEW_STATUSES), name="ck_entity_review"),
        Index("ix_entities_scan_start", "scan_id", "start_position"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scans.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    original_entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    detected_value: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    masked_value: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    start_position: Mapped[int] = mapped_column(Integer, nullable=False)
    end_position: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[list | None] = mapped_column(JSON)
    detection_method: Mapped[str] = mapped_column(String(32), nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provenance: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _now()

    scan: Mapped[Scan] = relationship(back_populates="entities")
    actions: Mapped[list["RedactionAction"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan", order_by="RedactionAction.decided_at"
    )


# Number of detected entities per scan, computed in SQL so list/detail views need no extra query.
Scan.entity_count = column_property(
    select(func.count(PiiEntity.id)).where(PiiEntity.scan_id == Scan.id).correlate_except(PiiEntity).scalar_subquery()
)


class RedactionAction(Base):
    """Append-only review history; latest row per entity is the active decision."""

    __tablename__ = "redaction_actions"
    __table_args__ = (
        CheckConstraint(_in("action", ACTIONS), name="ck_action_value"),
        Index("ix_actions_entity_decided", "entity_id", "decided_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pii_entities.id", ondelete="CASCADE"), nullable=False
    )
    decided_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    decided_at: Mapped[datetime] = _now()

    entity: Mapped[PiiEntity] = relationship(back_populates="actions")


class RedactedDocument(Base):
    __tablename__ = "redacted_documents"
    __table_args__ = (
        CheckConstraint(_in("validation_status", VALIDATION_STATUSES), name="ck_redacted_validation"),
        CheckConstraint("redaction_count >= 0", name="ck_redacted_count"),
        Index("ix_redacted_original", "original_document_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    original_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    source_scan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scans.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    redaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    validation_details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _now()


class DocumentReviewer(Base):
    """A reviewer the document owner has explicitly authorised to review this document."""

    __tablename__ = "document_reviewers"
    __table_args__ = (UniqueConstraint("document_id", "reviewer_id", name="uq_document_reviewer"),
                      Index("ix_document_reviewers_reviewer", "reviewer_id"))

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    reviewer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    assigned_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = _now()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_user_created", "user_id", "created_at"),
        Index("ix_audit_document", "document_id"),
        Index("ix_audit_seq", "seq"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()
    # Tamper-evident chain (gap 3)
    seq: Mapped[int | None] = mapped_column(BigInteger)  # monotonic; null only on old rows before migration
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    row_hash: Mapped[str | None] = mapped_column(String(64))


__all__ = [
    "User", "RefreshToken", "Document", "Scan", "PiiEntity",
    "RedactionAction", "RedactedDocument", "AuditLog", "DocumentReviewer",
]
