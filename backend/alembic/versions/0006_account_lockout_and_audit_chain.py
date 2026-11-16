"""account lockout + tamper-evident audit chain

Revision ID: 0006
Revises: 0005
"""
import hashlib
import json
import uuid as _uuid
from datetime import timezone

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Account lockout columns ─────────────────────────────────────────────
    op.add_column("users", sa.Column("failed_logins", sa.Integer(), nullable=True))
    op.execute("UPDATE users SET failed_logins = 0")
    op.alter_column("users", "failed_logins", nullable=False, server_default="0")

    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))

    # ── 2. Tamper-evident audit log ────────────────────────────────────────────
    op.add_column("audit_logs", sa.Column("seq", sa.BigInteger(), nullable=True))
    op.add_column("audit_logs", sa.Column("prev_hash", sa.String(64), nullable=True))
    op.add_column("audit_logs", sa.Column("row_hash", sa.String(64), nullable=True))

    # Back-fill the hash chain for existing rows (ordered by created_at, then id for stability).
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, user_id, document_id, action, details, request_id, created_at FROM audit_logs ORDER BY created_at, id")
    ).fetchall()

    prev_hash = "0" * 64  # genesis
    for seq_num, row in enumerate(rows, start=1):
        rid, user_id, document_id, action, details, request_id, created_at = row
        # Canonical JSON of details (None → null)
        details_json = json.dumps(details, sort_keys=True, default=str) if details is not None else "null"
        # Normalise created_at to an ISO string (timezone-aware if possible)
        if hasattr(created_at, "isoformat"):
            ts = created_at.isoformat()
        else:
            ts = str(created_at)
        payload = "|".join([
            prev_hash,
            str(rid),
            str(user_id) if user_id is not None else "",
            str(document_id) if document_id is not None else "",
            action or "",
            details_json,
            request_id or "",
            ts,
        ])
        row_hash = hashlib.sha256(payload.encode()).hexdigest()
        conn.execute(
            sa.text("UPDATE audit_logs SET seq = :seq, prev_hash = :prev_hash, row_hash = :row_hash WHERE id = :id"),
            {"seq": seq_num, "prev_hash": prev_hash, "id": rid, "row_hash": row_hash},
        )
        prev_hash = row_hash

    # Make seq non-nullable after back-fill (new rows are written with the chain in app code).
    op.create_index("ix_audit_seq", "audit_logs", ["seq"])


def downgrade() -> None:
    op.drop_index("ix_audit_seq", table_name="audit_logs")
    op.drop_column("audit_logs", "row_hash")
    op.drop_column("audit_logs", "prev_hash")
    op.drop_column("audit_logs", "seq")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_logins")
