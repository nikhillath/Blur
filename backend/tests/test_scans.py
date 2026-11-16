import uuid

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import AuditLog, Document, Scan
from app.services.scan_runner import recover_stale_scans
from tests import samples

DOCS = "/api/v1/documents"


@pytest.fixture
def alice(make_user, auth_headers):
    make_user("alice@example.com")
    return auth_headers("alice@example.com")


@pytest.fixture
def bob(make_user, auth_headers):
    make_user("bob@example.com")
    return auth_headers("bob@example.com")


def upload(client, hdr, name, data, mime):
    r = client.post(DOCS, headers=hdr, files={"file": (name, data, mime)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def scan(client, hdr, doc_id):
    r = client.post(f"{DOCS}/{doc_id}/scan", headers=hdr)
    assert r.status_code == 202, r.text
    return client.get(f"/api/v1/scans/{r.json()['id']}", headers=hdr).json()


@pytest.mark.parametrize(
    "name,maker,mime",
    [
        ("a.txt", samples.make_txt, "text/plain"),
        ("a.pdf", samples.make_pdf, "application/pdf"),
        ("a.docx", samples.make_docx, None),
    ],
)
def test_scan_extracts_text_end_to_end(client, alice, name, maker, mime):
    did = upload(client, alice, name, maker(), mime or "application/octet-stream")
    s = scan(client, alice, did)
    assert s["status"] == "COMPLETED", s
    assert s["detection_mode"] == "rules-only"  # no model configured: say so
    assert s["duration_ms"] is not None and s["completed_at"]
    assert s["extraction_info"]["char_count"] > 0
    t = client.get(f"/api/v1/scans/{s['id']}/text", headers=alice).json()
    assert "riya.sharma@example.com" in t["text"]
    assert t["pages"][0]["end"] <= len(t["text"])
    doc = client.get(f"{DOCS}/{did}", headers=alice).json()
    assert doc["status"] == "SCANNED" and doc["processed_at"]


def test_scan_ocr_image_end_to_end(client, alice):
    from tests.test_extraction import _text_image, needs_ocr  # noqa: F401

    from app.document_processing.ocr import ocr_available

    if not ocr_available():
        pytest.skip("OCR engine unavailable")
    did = upload(client, alice, "scan.png", _text_image(["Email: riya.sharma@example.com"]), "image/png")
    s = scan(client, alice, did)
    assert s["status"] == "COMPLETED" and s["extraction_info"]["ocr_pages"] == 1
    t = client.get(f"/api/v1/scans/{s['id']}/text", headers=alice).json()
    assert t["coord_space"] == "image_pixels" and "example" in t["text"]


def test_extracted_text_not_in_scan_or_document_payloads(client, alice):
    did = upload(client, alice, "a.txt", samples.make_txt(), "text/plain")
    s = scan(client, alice, did)
    blob = str(s) + str(client.get(f"{DOCS}/{did}", headers=alice).json()) + str(client.get(DOCS, headers=alice).json())
    assert "riya.sharma" not in blob.lower()


def test_failed_extraction_marks_scan_and_document_failed_then_retry(client, alice):
    bad_pdf = b"%PDF-1.4\n" + b"this is not a real pdf body " * 10
    did = upload(client, alice, "bad.pdf", bad_pdf, "application/pdf")
    s = scan(client, alice, did)
    assert s["status"] == "FAILED" and s["error_message"] == "the PDF could not be read"
    assert client.get(f"{DOCS}/{did}", headers=alice).json()["status"] == "FAILED"
    assert client.get(f"/api/v1/scans/{s['id']}/text", headers=alice).status_code == 409

    r = client.post(f"/api/v1/scans/{s['id']}/retry", headers=alice)
    assert r.status_code == 202 and r.json()["id"] != s["id"]
    scans = client.get(f"{DOCS}/{did}/scans", headers=alice).json()
    assert len(scans) == 2  # history preserved
    with SessionLocal() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "SCAN_RETRIED"))
        assert db.scalar(select(AuditLog).where(AuditLog.action == "SCAN_FAILED"))


def test_unexpected_error_is_sanitised_and_not_left_processing(client, alice, monkeypatch):
    from app.services import scan_runner

    def boom(*a, **k):
        raise RuntimeError("secret internal path C:\\data\\x")

    monkeypatch.setattr(scan_runner, "extract", boom)
    did = upload(client, alice, "a.txt", samples.make_txt(), "text/plain")
    s = scan(client, alice, did)
    assert s["status"] == "FAILED" and s["error_message"] == "processing failed"
    assert "secret" not in str(s)
    assert client.get(f"{DOCS}/{did}", headers=alice).json()["status"] == "FAILED"


def test_failed_rescan_does_not_hide_earlier_success(client, alice, monkeypatch, storage):
    did = upload(client, alice, "a.txt", samples.make_txt(), "text/plain")
    assert scan(client, alice, did)["status"] == "COMPLETED"
    for k in list(storage.iter_keys()):  # file vanishes -> second scan fails
        storage.delete(k)
    assert scan(client, alice, did)["status"] == "FAILED"
    assert client.get(f"{DOCS}/{did}", headers=alice).json()["status"] == "SCANNED"


def test_multiple_completed_scans_keep_separate_results(client, alice):
    did = upload(client, alice, "a.txt", samples.make_txt(), "text/plain")
    a = scan(client, alice, did)
    b = scan(client, alice, did)
    assert a["id"] != b["id"] and a["status"] == b["status"] == "COMPLETED"
    assert len(client.get(f"{DOCS}/{did}/scans", headers=alice).json()) == 2


def test_retry_only_failed_scans(client, alice):
    did = upload(client, alice, "a.txt", samples.make_txt(), "text/plain")
    s = scan(client, alice, did)
    assert client.post(f"/api/v1/scans/{s['id']}/retry", headers=alice).status_code == 409


def test_text_and_retry_enforce_ownership(client, alice, bob):
    did = upload(client, alice, "a.txt", samples.make_txt(), "text/plain")
    s = scan(client, alice, did)
    assert client.get(f"/api/v1/scans/{s['id']}/text", headers=bob).status_code == 404
    assert client.post(f"/api/v1/scans/{s['id']}/retry", headers=bob).status_code == 404
    assert client.get(f"/api/v1/scans/{s['id']}/text").status_code == 401


def test_text_view_is_audited(client, alice):
    did = upload(client, alice, "a.txt", samples.make_txt(), "text/plain")
    s = scan(client, alice, did)
    client.get(f"/api/v1/scans/{s['id']}/text", headers=alice)
    with SessionLocal() as db:
        row = db.scalar(select(AuditLog).where(AuditLog.action == "EXTRACTED_TEXT_VIEWED"))
        assert row and "riya" not in str(row.details).lower()


def test_recover_stale_scans_fails_stuck_scans(client, alice, monkeypatch):
    from app.services import scan_runner

    monkeypatch.setattr(scan_runner, "run_scan", lambda scan_id: None)  # leave scan PENDING
    did = upload(client, alice, "a.txt", samples.make_txt(), "text/plain")
    r = client.post(f"{DOCS}/{did}/scan", headers=alice)
    with SessionLocal() as db:
        s = db.get(Scan, uuid.UUID(r.json()["id"]))
        s.status = "PROCESSING"
        db.get(Document, uuid.UUID(did)).status = "PROCESSING"
        db.commit()
    assert recover_stale_scans(startup=True) == 1
    with SessionLocal() as db:
        s = db.get(Scan, uuid.UUID(r.json()["id"]))
        assert s.status == "FAILED" and "restart" in s.error_message
        assert db.get(Document, uuid.UUID(did)).status == "FAILED"
    assert recover_stale_scans(startup=True) == 0
    # and the user can scan again
    monkeypatch.undo()
    assert scan(client, alice, did)["status"] == "COMPLETED"
