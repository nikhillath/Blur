import pytest

from tests.test_detection_pipeline import FULL

DOCS = "/api/v1/documents"


@pytest.fixture
def alice(make_user, auth_headers):
    make_user("alice@example.com")
    return auth_headers("alice@example.com")


def test_dashboard_counts_come_from_real_data(client, alice, make_user, auth_headers):
    assert client.get("/api/v1/dashboard").status_code == 401
    empty = client.get("/api/v1/dashboard", headers=alice).json()
    assert empty["documents"] == 0 and empty["pending_entities"] == 0 and [a["action"] for a in empty["recent_activity"]] == ["LOGIN_SUCCEEDED"]

    did = client.post(DOCS, headers=alice, files={"file": ("a.txt", FULL.encode(), "text/plain")}).json()["id"]
    client.post(DOCS, headers=alice, files={"file": ("b.txt", b"nothing sensitive here", "text/plain")})
    sid = client.post(f"{DOCS}/{did}/scan", headers=alice).json()["id"]
    d = client.get("/api/v1/dashboard", headers=alice).json()
    assert d["documents"] == 2 and d["by_status"] == {"SCANNED": 1, "UPLOADED": 1}
    total = client.get(f"/api/v1/scans/{sid}/entities", headers=alice).json()["total"]
    assert d["pending_entities"] == total and d["awaiting_review_documents"] == 1
    assert d["recent_activity"][0]["action"] == "SCAN_COMPLETED"

    client.post(f"/api/v1/scans/{sid}/reviews/bulk", json={"action": "REDACT", "min_confidence": 0.0}, headers=alice)
    client.post(f"/api/v1/scans/{sid}/redact", headers=alice)
    d = client.get("/api/v1/dashboard", headers=alice).json()
    assert d["pending_entities"] == 0 and d["awaiting_review_documents"] == 0 and d["redacted_documents"] == 1

    make_user("bob@example.com")
    other = client.get("/api/v1/dashboard", headers=auth_headers("bob@example.com")).json()
    assert other["documents"] == 0 and all(a["document_id"] is None for a in other["recent_activity"])  # scoped to the caller


def test_dashboard_counts_failed_scans(client, alice):
    bad = b"%PDF-1.4\n" + b"not a real pdf " * 20
    did = client.post(DOCS, headers=alice, files={"file": ("x.pdf", bad, "application/pdf")}).json()["id"]
    client.post(f"{DOCS}/{did}/scan", headers=alice)
    d = client.get("/api/v1/dashboard", headers=alice).json()
    assert d["failed_scans"] == 1 and d["by_status"] == {"FAILED": 1}
