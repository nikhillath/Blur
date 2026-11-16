import hashlib
import threading
import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import AuditLog, Document, Scan
from tests import samples

DOCS = "/api/v1/documents"


def upload(client, hdr, name="record.txt", data=None, mime="text/plain"):
    data = samples.make_txt() if data is None else data
    return client.post(DOCS, headers=hdr, files={"file": (name, data, mime)})


@pytest.fixture(autouse=True)
def _no_background_scan(monkeypatch):
    """These tests cover the document/scan API contract; real scan execution is in test_scans.py."""
    from app.services import scan_runner

    monkeypatch.setattr(scan_runner, "run_scan", lambda scan_id: None)


@pytest.fixture
def alice(make_user, auth_headers):
    make_user("alice@example.com")
    return auth_headers("alice@example.com")


@pytest.fixture
def bob(make_user, auth_headers):
    make_user("bob@example.com")
    return auth_headers("bob@example.com")


# ---------- upload ----------

def test_upload_stores_file_and_metadata(client, alice, storage):
    data = samples.make_pdf()
    r = upload(client, alice, "report.pdf", data, "application/pdf")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["original_filename"] == "report.pdf"
    assert body["file_type"] == "pdf" and body["status"] == "UPLOADED"
    assert body["file_size"] == len(data)
    assert body["sha256"] == hashlib.sha256(data).hexdigest()
    assert "storage_key" not in body and "uploaded_by" not in body
    with SessionLocal() as db:
        doc = db.scalar(select(Document))
        assert storage.read(doc.storage_key) == data
        assert doc.storage_key.startswith("originals/") and "report" not in doc.storage_key


def test_upload_requires_auth(client):
    r = client.post(DOCS, files={"file": ("a.txt", b"hi", "text/plain")})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "name,data,mime,code",
    [
        ("a.exe", b"MZ", "application/octet-stream", 415),
        ("a.pdf", b"not a pdf", "application/pdf", 415),
        ("a.txt", b"", "text/plain", 422),
        ("a.pdf", samples.make_pdf(), "text/html", 415),
    ],
)
def test_upload_rejected_and_nothing_persisted(client, alice, storage, name, data, mime, code):
    r = upload(client, alice, name, data, mime)
    assert r.status_code == code, r.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Document)) == 0
    assert list(storage.iter_keys()) == []


def test_upload_too_large(client, alice, storage, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    r = upload(client, alice, "big.txt", b"a" * (1024 * 1024 + 10))
    assert r.status_code == 413
    assert list(storage.iter_keys()) == []


def test_upload_traversal_filename_is_harmless(client, alice, storage):
    r = upload(client, alice, "../../../evil.txt")
    assert r.status_code == 201
    assert r.json()["original_filename"] == "evil.txt"
    assert all(k.startswith("originals/") for k in storage.iter_keys())


def test_db_failure_leaves_no_orphan_file(client, alice, storage, monkeypatch):
    def boom(self):
        raise RuntimeError("db down")

    from app.api.v1 import documents as mod

    monkeypatch.setattr(mod, "log_event", lambda *a, **k: boom(None))
    with pytest.raises(RuntimeError):
        upload(client, alice)
    assert list(storage.iter_keys()) == []
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Document)) == 0


def test_storage_failure_creates_no_record(client, alice, monkeypatch):
    from app.storage.local import LocalStorage, StorageError

    def fail(self, key, data):
        raise StorageError("disk full")

    monkeypatch.setattr(LocalStorage, "save", fail)
    r = upload(client, alice)
    assert r.status_code == 500 and "disk" not in r.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Document)) == 0


# ---------- listing / detail ----------

def test_list_is_scoped_paginated_and_filterable(client, alice, bob):
    for i in range(3):
        upload(client, alice, f"a{i}.txt")
    upload(client, alice, "100%_done.txt")
    upload(client, bob, "bobs.txt")
    r = client.get(f"{DOCS}?page=1&page_size=2", headers=alice).json()
    assert r["total"] == 4 and len(r["items"]) == 2 and r["page"] == 1
    assert all("bobs" not in d["original_filename"] for d in r["items"])
    assert client.get(f"{DOCS}?q=a1", headers=alice).json()["total"] == 1
    assert client.get(f"{DOCS}?q=%25_", headers=alice).json()["total"] == 1  # LIKE wildcards are literal
    assert client.get(f"{DOCS}?status=SCANNED", headers=alice).json()["total"] == 0
    assert client.get(f"{DOCS}?status=bogus", headers=alice).status_code == 422
    assert client.get(f"{DOCS}?page=0", headers=alice).status_code == 422
    assert client.get(f"{DOCS}?page_size=1000", headers=alice).status_code == 422


def test_list_includes_scan_summary(client, alice):
    doc = upload(client, alice).json()
    client.post(f"{DOCS}/{doc['id']}/scan", headers=alice)
    item = client.get(DOCS, headers=alice).json()["items"][0]
    assert item["scan_count"] == 1 and item["latest_scan"]["status"] == "PENDING"


def test_detail_includes_scans(client, alice):
    doc = upload(client, alice).json()
    d = client.get(f"{DOCS}/{doc['id']}", headers=alice)
    assert d.status_code == 200 and d.json()["scans"] == []


# ---------- authorization (IDOR) ----------

def test_other_users_cannot_access_documents(client, alice, bob):
    doc = upload(client, alice).json()
    did = doc["id"]
    scan = client.post(f"{DOCS}/{did}/scan", headers=alice).json()
    for method, url in [
        ("get", f"{DOCS}/{did}"),
        ("get", f"{DOCS}/{did}/download"),
        ("get", f"{DOCS}/{did}/scans"),
        ("post", f"{DOCS}/{did}/scan"),
        ("delete", f"{DOCS}/{did}"),
        ("get", f"/api/v1/scans/{scan['id']}"),
    ]:
        assert getattr(client, method)(url, headers=bob).status_code == 404, url
    # still intact for owner
    assert client.get(f"{DOCS}/{did}", headers=alice).status_code == 200


def test_admin_does_not_get_blanket_document_access(client, make_user, auth_headers, alice):
    doc = upload(client, alice).json()
    make_user("admin@example.com", "ADMIN")
    adm = auth_headers("admin@example.com")
    assert client.get(f"{DOCS}/{doc['id']}", headers=adm).status_code == 404


def test_unauthenticated_access_denied(client, alice):
    did = upload(client, alice).json()["id"]
    for url in (DOCS, f"{DOCS}/{did}", f"{DOCS}/{did}/download", f"{DOCS}/{did}/scans"):
        assert client.get(url).status_code == 401


def test_unknown_and_malformed_ids(client, alice):
    assert client.get(f"{DOCS}/{uuid.uuid4()}", headers=alice).status_code == 404
    assert client.get(f"{DOCS}/not-a-uuid", headers=alice).status_code == 422


# ---------- download ----------

def test_download_returns_original_bytes_with_safe_headers(client, alice):
    data = samples.make_pdf()
    did = upload(client, alice, "my report.pdf", data, "application/pdf").json()["id"]
    r = client.get(f"{DOCS}/{did}/download", headers=alice)
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "private" in r.headers["cache-control"]


def test_download_missing_blob_is_404(client, alice, storage):
    did = upload(client, alice).json()["id"]
    for k in list(storage.iter_keys()):
        storage.delete(k)
    r = client.get(f"{DOCS}/{did}/download", headers=alice)
    assert r.status_code == 404 and str(storage.root) not in r.text


# ---------- delete ----------

def test_delete_removes_record_scans_and_files(client, alice, storage):
    did = upload(client, alice).json()["id"]
    client.post(f"{DOCS}/{did}/scan", headers=alice)
    assert client.delete(f"{DOCS}/{did}", headers=alice).status_code == 204
    assert client.get(f"{DOCS}/{did}", headers=alice).status_code == 404
    assert list(storage.iter_keys()) == []
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Scan)) == 0
        assert db.scalar(select(AuditLog).where(AuditLog.action == "DOCUMENT_DELETED"))


# ---------- scans ----------

def test_scan_lifecycle_and_single_active_scan(client, alice):
    did = upload(client, alice).json()["id"]
    r = client.post(f"{DOCS}/{did}/scan", headers=alice)
    assert r.status_code == 202
    scan = r.json()
    assert scan["status"] == "PENDING" and scan["document_id"] == did
    assert "extracted_text" not in scan
    assert client.post(f"{DOCS}/{did}/scan", headers=alice).status_code == 409
    assert client.get(f"/api/v1/scans/{scan['id']}", headers=alice).json()["id"] == scan["id"]
    assert len(client.get(f"{DOCS}/{did}/scans", headers=alice).json()) == 1
    # a finished scan allows another; history is preserved
    with SessionLocal() as db:
        s = db.get(Scan, uuid.UUID(scan["id"]))
        s.status = "COMPLETED"
        db.commit()
    assert client.post(f"{DOCS}/{did}/scan", headers=alice).status_code == 202
    assert len(client.get(f"{DOCS}/{did}/scans", headers=alice).json()) == 2


def test_concurrent_scan_requests_create_one_scan(client, alice):
    did = upload(client, alice).json()["id"]
    results = []

    def go():
        results.append(client.post(f"{DOCS}/{did}/scan", headers=alice).status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [202] + [409] * 5
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Scan)) == 1


# ---------- audit ----------

def test_audit_events_recorded_without_pii(client, alice):
    did = upload(client, alice, "record.txt").json()["id"]
    client.post(f"{DOCS}/{did}/scan", headers=alice)
    client.get(f"{DOCS}/{did}/download", headers=alice)
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.document_id == uuid.UUID(did))).all()
        assert {"DOCUMENT_UPLOADED", "SCAN_REQUESTED", "DOCUMENT_DOWNLOADED"} <= {r.action for r in rows}
        blob = str([r.details for r in rows])
        assert "riya" not in blob.lower() and "98765" not in blob


def test_body_size_precheck_rejects_huge_content_length(client, alice, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    r = client.post(DOCS, headers={**alice, "Content-Length": str(50 * 1024 * 1024)}, content=b"x")
    assert r.status_code == 413
