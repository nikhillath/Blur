import os

# Point the app at the isolated test database BEFORE any app.db import.
from app.core.config import BACKEND_DIR, get_settings

_s = get_settings()
if not _s.test_database_url:
    raise RuntimeError("TEST_DATABASE_URL must be set; refusing to run tests against the dev database")
os.environ["DATABASE_URL"] = _s.test_database_url
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["OCR_WARMUP"] = "false"
from cryptography.fernet import Fernet  # noqa: E402

os.environ["ENCRYPTION_KEYS"] = Fernet.generate_key().decode()  # the whole suite runs with encryption at rest ON
os.environ["NER_MODEL"] = ""  # API tests run rules-only; model tests build their own detector
import tempfile  # noqa: E402

_STORAGE_TMP = tempfile.mkdtemp(prefix="pii_test_storage_")
os.environ["STORAGE_DIR"] = _STORAGE_TMP
get_settings.cache_clear()

import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.security.passwords import hash_password  # noqa: E402

assert engine.url.database.endswith("_test"), "tests must run against a *_test database"

PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def _clean_db():
    yield
    for child in Path(_STORAGE_TMP).iterdir():
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE audit_logs, redacted_documents, redaction_actions, pii_entities, "
                "scans, documents, refresh_tokens, users RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture(scope="session", autouse=True)
def _cleanup_storage():
    yield
    shutil.rmtree(_STORAGE_TMP, ignore_errors=True)


@pytest.fixture
def storage():
    from app.storage.local import get_storage

    return get_storage()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_user():
    def _make(email: str, role: str = "USER", active: bool = True) -> User:
        with SessionLocal() as db:
            u = User(name=email.split("@")[0], email=email, role=role,
                     password_hash=hash_password(PASSWORD), is_active=active)
            db.add(u)
            db.commit()
            return u

    return _make


@pytest.fixture
def auth_headers(client):
    def _login(email: str) -> dict:
        r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    return _login
