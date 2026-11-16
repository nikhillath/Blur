"""Prepares the E2E database: wipes it, applies migrations, creates an admin and a reviewer (fictional accounts)."""
import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import BACKEND_DIR
from app.db.session import SessionLocal, engine
from app.models import User
from app.security.passwords import hash_password

if not engine.url.database.endswith(("_test", "_e2e")):
    sys.exit("refusing to wipe a database that is not *_test or *_e2e")
command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
with engine.begin() as conn:
    conn.execute(text("TRUNCATE audit_logs, redacted_documents, redaction_actions, pii_entities, scans, documents, "
                      "document_reviewers, refresh_tokens, users RESTART IDENTITY CASCADE"))
pw = os.environ.get("E2E_PASSWORD", "e2e-password-123")
with SessionLocal() as db:
    db.add_all([User(name="E2E Admin", email="admin-e2e@example.com", role="ADMIN", password_hash=hash_password(pw)),
                User(name="E2E Reviewer", email="reviewer-e2e@example.com", role="REVIEWER", password_hash=hash_password(pw))])
    db.commit()
print("e2e database ready")
