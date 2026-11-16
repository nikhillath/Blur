import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models import PiiEntity, Scan
from app.security.tokens import utcnow
from app.services import scan_runner
from tests.test_detection_pipeline import FULL

DOCS = "/api/v1/documents"


@pytest.fixture
def alice(make_user, auth_headers):
    make_user("alice@example.com")
    return auth_headers("alice@example.com")


@pytest.fixture
def bob(make_user, auth_headers):
    make_user("bob@example.com")
    return auth_headers("bob@example.com")


def upload_scan(client, hdr, text=FULL):
    r = client.post(DOCS, headers=hdr, files={"file": ("d.txt", text.encode(), "text/plain")})
    assert r.status_code == 201
    did = r.json()["id"]
    s = client.post(f"{DOCS}/{did}/scan", headers=hdr).json()
    return did, client.get(f"/api/v1/scans/{s['id']}", headers=hdr).json()


def test_scan_stores_entities_and_reports_mode(client, alice):
    did, scan = upload_scan(client, alice)
    assert scan["status"] == "COMPLETED"
    assert scan["detection_mode"] == "rules-only" and scan["model_version"] == "rules-1.0"
    assert scan["entity_count"] >= 8
    assert scan["detector_config"]["rules"] == "rules-1.0"

    page = client.get(f"/api/v1/scans/{scan['id']}/entities?page_size=200", headers=alice).json()
    assert page["total"] == scan["entity_count"]
    assert {"PERSON", "EMAIL", "PHONE", "PAN", "AADHAAR", "DATE_OF_BIRTH"} <= set(page["counts_by_type"])
    starts = [e["start_position"] for e in page["items"]]
    assert starts == sorted(starts)
    for e in page["items"]:
        assert e["review_status"] == "PENDING" and e["is_manual"] is False
        assert e["original_entity_type"] == e["entity_type"]
        assert 0 <= e["confidence_score"] <= 1 and e["provenance"]


def test_api_never_returns_raw_values(client, alice):
    _, scan = upload_scan(client, alice)
    body = client.get(f"/api/v1/scans/{scan['id']}/entities?page_size=200", headers=alice).text
    assert "detected_value" not in body
    for secret in ("riya.sharma@example.com", "98765 43210", "ABCDE1234F", "4521 7834 9123"):
        assert secret not in body


def test_stored_offsets_match_extracted_text(client, alice):
    _, scan = upload_scan(client, alice)
    text = client.get(f"/api/v1/scans/{scan['id']}/text", headers=alice).json()["text"]
    with SessionLocal() as db:
        rows = db.scalars(select(PiiEntity)).all()
        assert rows
        for r in rows:
            assert text[r.start_position : r.end_position] == r.detected_value
            assert r.masked_value != r.detected_value


def test_filters_and_pagination(client, alice):
    _, scan = upload_scan(client, alice)
    base = f"/api/v1/scans/{scan['id']}/entities"
    only_email = client.get(f"{base}?entity_type=EMAIL", headers=alice).json()
    assert only_email["total"] == 1 and only_email["items"][0]["entity_type"] == "EMAIL"
    assert only_email["counts_by_type"]["PERSON"] >= 1  # counts ignore the filter
    hi = client.get(f"{base}?min_confidence=0.9", headers=alice).json()
    assert hi["total"] < scan["entity_count"] and all(e["confidence_score"] >= 0.9 for e in hi["items"])
    p1 = client.get(f"{base}?page=1&page_size=3", headers=alice).json()
    p2 = client.get(f"{base}?page=2&page_size=3", headers=alice).json()
    assert len(p1["items"]) == 3 and {e["id"] for e in p1["items"]}.isdisjoint({e["id"] for e in p2["items"]})
    assert client.get(f"{base}?entity_type=BOGUS", headers=alice).status_code == 422
    assert client.get(f"{base}?review_status=MAYBE", headers=alice).status_code == 422
    assert client.get(f"{base}?page_size=500", headers=alice).status_code == 422
    assert client.get(f"{base}?review_status=REDACT", headers=alice).json()["total"] == 0


def test_entities_enforce_ownership(client, alice, bob):
    _, scan = upload_scan(client, alice)
    url = f"/api/v1/scans/{scan['id']}/entities"
    assert client.get(url, headers=bob).status_code == 404
    assert client.get(url).status_code == 401


def test_document_without_pii_completes_with_zero_entities(client, alice):
    _, scan = upload_scan(client, alice, "The quarterly report shows revenue grew steadily.")
    assert scan["status"] == "COMPLETED" and scan["entity_count"] == 0


def test_rescans_keep_separate_entity_sets(client, alice):
    did, first = upload_scan(client, alice)
    second = client.post(f"{DOCS}/{did}/scan", headers=alice).json()
    n1 = client.get(f"/api/v1/scans/{first['id']}/entities", headers=alice).json()["total"]
    n2 = client.get(f"/api/v1/scans/{second['id']}/entities", headers=alice).json()["total"]
    assert n1 == n2 > 0
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(PiiEntity)) == n1 + n2


def test_detection_failure_is_atomic(client, alice, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("detector crashed on C:\\secret")

    monkeypatch.setattr(scan_runner, "run_detection", boom)
    _, scan = upload_scan(client, alice)
    assert scan["status"] == "FAILED" and scan["error_message"] == "processing failed" and scan["entity_count"] == 0
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(PiiEntity)) == 0
        s = db.get(Scan, uuid.UUID(scan["id"]))
        assert s.extracted_text is None  # no half-written result


def test_deleting_document_removes_entities(client, alice):
    did, _ = upload_scan(client, alice)
    assert client.delete(f"{DOCS}/{did}", headers=alice).status_code == 204
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(PiiEntity)) == 0


# ------------------------------------------------------- stale-scan handling and atomic claim
def _pending_scan(client, hdr, monkeypatch):
    monkeypatch.setattr(scan_runner, "run_scan", lambda scan_id: None)
    r = client.post(DOCS, headers=hdr, files={"file": ("d.txt", FULL.encode(), "text/plain")})
    did = r.json()["id"]
    sid = client.post(f"{DOCS}/{did}/scan", headers=hdr).json()["id"]
    return did, uuid.UUID(sid)


def test_old_active_scan_is_auto_failed_so_new_scan_can_start(client, alice, monkeypatch):
    did, sid = _pending_scan(client, alice, monkeypatch)
    assert client.post(f"{DOCS}/{did}/scan", headers=alice).status_code == 409  # fresh: still blocks
    with SessionLocal() as db:
        db.get(Scan, sid).started_at = utcnow() - timedelta(hours=1)
        db.commit()
    monkeypatch.undo()
    assert client.post(f"{DOCS}/{did}/scan", headers=alice).status_code == 202
    with SessionLocal() as db:
        old = db.get(Scan, sid)
        assert old.status == "FAILED" and old.error_message == "scan timed out"


def test_recovery_spares_fresh_scans_unless_startup(client, alice, monkeypatch):
    _, sid = _pending_scan(client, alice, monkeypatch)
    assert scan_runner.recover_stale_scans() == 0  # an in-flight scan of another worker is left alone
    assert scan_runner.recover_stale_scans(startup=True) == 1
    with SessionLocal() as db:
        assert db.get(Scan, sid).status == "FAILED"


def test_run_scan_claims_exactly_once(client, alice, monkeypatch):
    _, sid = _pending_scan(client, alice, monkeypatch)
    monkeypatch.undo()
    scan_runner.run_scan(sid)
    scan_runner.run_scan(sid)  # second pickup must be a no-op
    with SessionLocal() as db:
        n = db.scalar(select(func.count()).select_from(PiiEntity))
        assert db.get(Scan, sid).status == "COMPLETED" and n > 0
    scan_runner.run_scan(sid)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(PiiEntity)) == n  # no duplicates
        db.get(Scan, sid).status = "PROCESSING"
        db.commit()
    scan_runner.run_scan(sid)  # a PROCESSING scan is never re-run
    with SessionLocal() as db:
        assert db.get(Scan, sid).status == "PROCESSING"
