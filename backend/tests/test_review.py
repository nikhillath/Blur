import threading
import uuid

import pytest
from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models import AuditLog, PiiEntity, RedactionAction
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


@pytest.fixture
def rita(make_user, auth_headers):
    make_user("rita@example.com", "REVIEWER")
    return auth_headers("rita@example.com")


@pytest.fixture
def scan(client, alice):
    r = client.post(DOCS, headers=alice, files={"file": ("d.txt", FULL.encode(), "text/plain")})
    did = r.json()["id"]
    sid = client.post(f"{DOCS}/{did}/scan", headers=alice).json()["id"]
    assert client.get(f"/api/v1/scans/{sid}", headers=alice).json()["status"] == "COMPLETED"
    return {"doc": did, "id": sid}


def entities(client, hdr, sid, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/api/v1/scans/{sid}/entities?page_size=200&{q}", headers=hdr).json()


def first(client, hdr, sid, etype):
    return next(e for e in entities(client, hdr, sid)["items"] if e["entity_type"] == etype)


# ---------------------------------------------------------------------------- single decisions
def test_decision_persists_and_history_is_append_only(client, alice, scan):
    e = first(client, alice, scan["id"], "EMAIL")
    assert e["review_status"] == "PENDING"
    url = f"/api/v1/entities/{e['id']}"
    r = client.post(f"{url}/review", json={"action": "REDACT", "reason": "customer contact"}, headers=alice)
    assert r.status_code == 200 and r.json()["review_status"] == "REDACT"
    client.post(f"{url}/review", json={"action": "KEEP"}, headers=alice)  # revise the decision
    client.post(f"{url}/review", json={"action": "FALSE_POSITIVE", "reason": "test address"}, headers=alice)
    hist = client.get(f"{url}/history", headers=alice).json()
    assert [h["action"] for h in hist] == ["REDACT", "KEEP", "FALSE_POSITIVE"]  # nothing overwritten
    assert hist[0]["reason"] == "customer contact" and hist[0]["decided_by_name"] == "alice"
    cur = entities(client, alice, scan["id"], entity_type="EMAIL")["items"][0]
    assert cur["review_status"] == "FALSE_POSITIVE"  # latest decision wins
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(RedactionAction)) == 3


def test_invalid_action_and_unknown_entity(client, alice, scan):
    e = first(client, alice, scan["id"], "EMAIL")
    assert client.post(f"/api/v1/entities/{e['id']}/review", json={"action": "DELETE"}, headers=alice).status_code == 422
    assert client.post(f"/api/v1/entities/{e['id']}/review", json={}, headers=alice).status_code == 422
    assert client.post(f"/api/v1/entities/{uuid.uuid4()}/review", json={"action": "KEEP"}, headers=alice).status_code == 404
    assert client.post(f"/api/v1/entities/{e['id']}/review", json={"action": "KEEP", "reason": "x" * 501}, headers=alice).status_code == 422


def test_review_progress_counts(client, alice, scan):
    page = entities(client, alice, scan["id"])
    total = page["total"]
    assert page["counts_by_status"] == {"PENDING": total}
    ids = [e["id"] for e in page["items"][:3]]
    for i, action in zip(ids, ["REDACT", "KEEP", "FALSE_POSITIVE"]):
        client.post(f"/api/v1/entities/{i}/review", json={"action": action}, headers=alice)
    c = entities(client, alice, scan["id"])["counts_by_status"]
    assert c == {"PENDING": total - 3, "REDACT": 1, "KEEP": 1, "FALSE_POSITIVE": 1}


# ---------------------------------------------------------------------------- authorisation
def test_other_users_cannot_touch_entities(client, alice, bob, scan):
    e = first(client, alice, scan["id"], "EMAIL")
    url = f"/api/v1/entities/{e['id']}"
    assert client.post(f"{url}/review", json={"action": "REDACT"}, headers=bob).status_code == 404
    assert client.patch(url, json={"entity_type": "PHONE"}, headers=bob).status_code == 404
    assert client.get(f"{url}/history", headers=bob).status_code == 404
    assert client.delete(url, headers=bob).status_code == 404
    sid = scan["id"]
    assert client.post(f"/api/v1/scans/{sid}/reviews/bulk", json={"action": "REDACT", "entity_type": "EMAIL"}, headers=bob).status_code == 404
    assert client.post(f"/api/v1/scans/{sid}/entities", json={"start": 0, "end": 5, "entity_type": "PERSON"}, headers=bob).status_code == 404
    for u in (f"/api/v1/scans/{sid}", f"/api/v1/scans/{sid}/text", f"/api/v1/scans/{sid}/entities"):
        assert client.get(u, headers=bob).status_code == 404
    assert entities(client, alice, sid, entity_type="EMAIL")["items"][0]["review_status"] == "PENDING"


def test_unauthenticated_review_rejected(client, alice, scan):
    e = first(client, alice, scan["id"], "EMAIL")
    assert client.post(f"/api/v1/entities/{e['id']}/review", json={"action": "KEEP"}).status_code == 401


def test_assigned_reviewer_can_review_but_not_own_the_document(client, alice, rita, scan):
    e = first(client, alice, scan["id"], "PAN")
    url = f"/api/v1/entities/{e['id']}"
    assert client.post(f"{url}/review", json={"action": "REDACT"}, headers=rita).status_code == 404  # not assigned yet
    r = client.post(f"{DOCS}/{scan['doc']}/reviewers", json={"email": "Rita@Example.com"}, headers=alice)
    assert r.status_code == 201 and r.json()["email"] == "rita@example.com"
    assert client.post(f"{url}/review", json={"action": "REDACT", "reason": "approved"}, headers=rita).status_code == 200
    assert client.get(f"/api/v1/scans/{scan['id']}/text", headers=rita).status_code == 200
    assert entities(client, rita, scan["id"])["total"] > 0
    hist = client.get(f"{url}/history", headers=alice).json()
    assert hist[-1]["decided_by_name"] == "rita"  # the owner sees who decided
    # a reviewer reviews; they do not manage the document
    assert client.get(f"{DOCS}/{scan['doc']}", headers=rita).status_code == 404
    assert client.get(f"{DOCS}/{scan['doc']}/download", headers=rita).status_code == 404
    assert client.delete(f"{DOCS}/{scan['doc']}", headers=rita).status_code == 404
    assert client.post(f"{DOCS}/{scan['doc']}/scan", headers=rita).status_code == 404
    assert client.post(f"/api/v1/scans/{scan['id']}/retry", headers=rita).status_code == 404
    assert client.post(f"{DOCS}/{scan['doc']}/reviewers", json={"email": "alice@example.com"}, headers=rita).status_code == 404
    mine = client.get("/api/v1/review/assigned", headers=rita).json()
    assert mine["total"] == 1 and mine["items"][0]["id"] == scan["doc"]


def test_unassigning_revokes_access(client, alice, rita, scan):
    client.post(f"{DOCS}/{scan['doc']}/reviewers", json={"email": "rita@example.com"}, headers=alice)
    uid = client.get(f"{DOCS}/{scan['doc']}/reviewers", headers=alice).json()[0]["user_id"]
    assert client.get(f"/api/v1/scans/{scan['id']}/entities", headers=rita).status_code == 200
    assert client.delete(f"{DOCS}/{scan['doc']}/reviewers/{uid}", headers=alice).status_code == 204
    assert client.get(f"/api/v1/scans/{scan['id']}/entities", headers=rita).status_code == 404
    assert client.get("/api/v1/review/assigned", headers=rita).json()["total"] == 0
    assert client.delete(f"{DOCS}/{scan['doc']}/reviewers/{uid}", headers=alice).status_code == 404


def test_assignment_rules(client, alice, bob, make_user, scan):
    base = f"{DOCS}/{scan['doc']}/reviewers"
    assert client.post(base, json={"email": "bob@example.com"}, headers=alice).status_code == 422  # plain USER
    assert client.post(base, json={"email": "nobody@example.com"}, headers=alice).status_code == 422
    assert client.post(base, json={"email": "alice@example.com"}, headers=alice).status_code == 422  # self
    make_user("off@example.com", "REVIEWER", active=False)
    assert client.post(base, json={"email": "off@example.com"}, headers=alice).status_code == 422
    make_user("rita@example.com", "REVIEWER")
    assert client.post(base, json={"email": "rita@example.com"}, headers=alice).status_code == 201
    assert client.post(base, json={"email": "rita@example.com"}, headers=alice).status_code == 409
    assert client.get(base, headers=bob).status_code == 404  # only the owner sees the reviewer list
    assert client.post(base, json={"email": "rita@example.com"}).status_code == 401


def test_demoted_reviewer_loses_access_despite_assignment(client, alice, rita, make_user, auth_headers, scan):
    client.post(f"{DOCS}/{scan['doc']}/reviewers", json={"email": "rita@example.com"}, headers=alice)
    make_user("admin@example.com", "ADMIN")
    adm = auth_headers("admin@example.com")
    with SessionLocal() as db:
        from app.models import User

        uid = db.scalar(select(User.id).where(User.email == "rita@example.com"))
    assert client.patch(f"/api/v1/users/{uid}", json={"role": "USER"}, headers=adm).status_code == 200
    assert client.get(f"/api/v1/scans/{scan['id']}/entities", headers=rita).status_code == 404


def test_admin_has_no_blanket_review_access(client, alice, make_user, auth_headers, scan):
    make_user("admin@example.com", "ADMIN")
    assert client.get(f"/api/v1/scans/{scan['id']}/entities", headers=auth_headers("admin@example.com")).status_code == 404


# ---------------------------------------------------------------------------- type correction
def test_type_correction_keeps_original_and_remasks(client, alice, scan):
    e = first(client, alice, scan["id"], "AADHAAR")
    r = client.patch(f"/api/v1/entities/{e['id']}", json={"entity_type": "BANK_ACCOUNT"}, headers=alice)
    assert r.status_code == 200
    body = r.json()
    assert body["entity_type"] == "BANK_ACCOUNT" and body["original_entity_type"] == "AADHAAR"
    assert body["masked_value"] != e["masked_value"] or body["masked_value"].endswith("9123")
    assert any("correction" in p for p in body["provenance"])  # model prediction + correction both preserved
    assert client.patch(f"/api/v1/entities/{e['id']}", json={"entity_type": "NOPE"}, headers=alice).status_code == 422
    assert client.patch(f"/api/v1/entities/{e['id']}", json={}, headers=alice).status_code == 422
    with SessionLocal() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "ENTITY_TYPE_CORRECTED"))
    # no-op correction writes nothing new
    client.patch(f"/api/v1/entities/{e['id']}", json={"entity_type": "BANK_ACCOUNT"}, headers=alice)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "ENTITY_TYPE_CORRECTED")) == 1


# ---------------------------------------------------------------------------- bulk
def test_bulk_by_ids(client, alice, scan):
    page = entities(client, alice, scan["id"])
    ids = [e["id"] for e in page["items"][:4]]
    r = client.post(f"/api/v1/scans/{scan['id']}/reviews/bulk", json={"action": "REDACT", "entity_ids": ids}, headers=alice)
    assert r.status_code == 200 and r.json() == {"updated": 4, "skipped": 0}
    assert entities(client, alice, scan["id"])["counts_by_status"]["REDACT"] == 4


def test_bulk_by_filter_only_touches_pending(client, alice, scan):
    sid = scan["id"]
    people = entities(client, alice, sid, entity_type="PERSON")["items"]
    client.post(f"/api/v1/entities/{people[0]['id']}/review", json={"action": "KEEP"}, headers=alice)
    r = client.post(f"/api/v1/scans/{sid}/reviews/bulk", json={"action": "REDACT", "entity_type": "PERSON"}, headers=alice)
    assert r.json()["updated"] == len(people) - 1
    kept = entities(client, alice, sid, review_status="KEEP")["items"]
    assert [k["id"] for k in kept] == [people[0]["id"]]  # the earlier human decision survived
    r = client.post(f"/api/v1/scans/{sid}/reviews/bulk", json={"action": "REDACT", "entity_type": "PERSON", "only_pending": False}, headers=alice)
    assert r.json()["updated"] == len(people)


def test_bulk_confidence_band(client, alice, scan):
    sid = scan["id"]
    low = [e for e in entities(client, alice, sid)["items"] if e["confidence_score"] < 0.8]
    assert low
    r = client.post(f"/api/v1/scans/{sid}/reviews/bulk", json={"action": "FALSE_POSITIVE", "max_confidence": 0.79}, headers=alice)
    assert r.json()["updated"] == len(low)


def test_bulk_validation(client, alice, scan):
    url = f"/api/v1/scans/{scan['id']}/reviews/bulk"
    assert client.post(url, json={"action": "REDACT"}, headers=alice).status_code == 422  # no selector
    assert client.post(url, json={"action": "REDACT", "entity_ids": [str(uuid.uuid4())], "entity_type": "PAN"}, headers=alice).status_code == 422
    assert client.post(url, json={"action": "REDACT", "entity_type": "BOGUS"}, headers=alice).status_code == 422
    assert client.post(url, json={"action": "REDACT", "entity_ids": [str(uuid.uuid4()) for _ in range(2001)]}, headers=alice).status_code == 422
    assert client.post(url, json={"action": "REDACT", "min_confidence": 2}, headers=alice).status_code == 422


def test_bulk_ignores_entities_from_other_scans(client, alice, bob, scan):
    r = client.post(DOCS, headers=bob, files={"file": ("b.txt", FULL.encode(), "text/plain")})
    bsid = client.post(f"{DOCS}/{r.json()['id']}/scan", headers=bob).json()["id"]
    foreign = first(client, bob, bsid, "EMAIL")["id"]
    own = first(client, alice, scan["id"], "EMAIL")["id"]
    res = client.post(f"/api/v1/scans/{scan['id']}/reviews/bulk", json={"action": "REDACT", "entity_ids": [own, foreign]}, headers=alice)
    assert res.json() == {"updated": 1, "skipped": 1}
    assert first(client, bob, bsid, "EMAIL")["review_status"] == "PENDING"  # bob's entity untouched


def test_bulk_is_audited_without_values(client, alice, scan):
    client.post(f"/api/v1/scans/{scan['id']}/reviews/bulk", json={"action": "REDACT", "entity_type": "EMAIL"}, headers=alice)
    with SessionLocal() as db:
        row = db.scalar(select(AuditLog).where(AuditLog.action == "ENTITIES_BULK_REVIEWED"))
        assert row.details["updated"] == 1 and "riya" not in str(row.details).lower()


# ---------------------------------------------------------------------------- manual entities
def test_manual_entity_lifecycle(client, alice, scan):
    sid = scan["id"]
    text = client.get(f"/api/v1/scans/{sid}/text", headers=alice).json()["text"]
    start = text.index("fictional")
    end = start + len("fictional")
    r = client.post(f"/api/v1/scans/{sid}/entities", json={"start": start, "end": end, "entity_type": "OTHER_IDENTIFIER", "reason": "internal code"}, headers=alice)
    assert r.status_code == 201
    m = r.json()
    assert m["is_manual"] and m["detection_method"] == "manual" and m["confidence_score"] == 1.0
    assert m["review_status"] == "REDACT"  # adding is itself a decision to redact
    assert m["start_position"] == start and m["masked_value"] != "fictional"
    with SessionLocal() as db:
        e = db.get(PiiEntity, uuid.UUID(m["id"]))
        assert e.detected_value == text[start:end]
    hist = client.get(f"/api/v1/entities/{m['id']}/history", headers=alice).json()
    assert hist[0]["action"] == "REDACT" and hist[0]["reason"] == "internal code"
    assert any(x["id"] == m["id"] for x in entities(client, alice, sid)["items"])
    assert client.post(f"/api/v1/scans/{sid}/entities", json={"start": start, "end": end, "entity_type": "OTHER_IDENTIFIER"}, headers=alice).status_code == 409
    assert client.delete(f"/api/v1/entities/{m['id']}", headers=alice).status_code == 204
    assert not any(x["id"] == m["id"] for x in entities(client, alice, sid)["items"])


@pytest.mark.parametrize("payload", [
    {"start": 5, "end": 5, "entity_type": "PERSON"},
    {"start": 10, "end": 5, "entity_type": "PERSON"},
    {"start": -1, "end": 5, "entity_type": "PERSON"},
    {"start": 0, "end": 10**6, "entity_type": "PERSON"},
    {"start": 0, "end": 5, "entity_type": "NOPE"},
    {"start": 26, "end": 28, "entity_type": "PERSON"},   # lands on whitespace/newline
    {"start": 0, "end": 5},
])
def test_manual_entity_validation(client, alice, scan, payload):
    r = client.post(f"/api/v1/scans/{scan['id']}/entities", json=payload, headers=alice)
    assert r.status_code == 422, r.text


def test_manual_span_rejects_padding_and_overlong(client, alice, scan):
    text = client.get(f"/api/v1/scans/{scan['id']}/text", headers=alice).json()["text"]
    i = text.index("Riya")
    assert client.post(f"/api/v1/scans/{scan['id']}/entities", json={"start": i, "end": i + 5, "entity_type": "PERSON"}, headers=alice).status_code == 422  # "Riya "
    assert client.post(f"/api/v1/scans/{scan['id']}/entities", json={"start": 0, "end": len(text), "entity_type": "ADDRESS"}, headers=alice).status_code == 422


def test_model_entities_cannot_be_deleted(client, alice, scan):
    e = first(client, alice, scan["id"], "EMAIL")
    assert client.delete(f"/api/v1/entities/{e['id']}", headers=alice).status_code == 409


def test_manual_entity_has_location_on_pdf(client, alice):
    from tests import samples

    r = client.post(DOCS, headers=alice, files={"file": ("d.pdf", samples.make_pdf(), "application/pdf")})
    sid = client.post(f"{DOCS}/{r.json()['id']}/scan", headers=alice).json()["id"]
    text = client.get(f"/api/v1/scans/{sid}/text", headers=alice).json()["text"]
    i = text.index("Customer")
    m = client.post(f"/api/v1/scans/{sid}/entities", json={"start": i, "end": i + 8, "entity_type": "OTHER_IDENTIFIER"}, headers=alice).json()
    assert m["page_number"] == 1 and m["bbox"] and m["bbox"][0][0] == 1


def test_cannot_review_a_failed_scan(client, alice, scan):
    from app.models import Scan

    with SessionLocal() as db:
        s = db.get(Scan, uuid.UUID(scan["id"]))
        s.status = "FAILED"
        db.commit()
    url = f"/api/v1/scans/{scan['id']}"
    assert client.post(f"{url}/reviews/bulk", json={"action": "REDACT", "entity_type": "EMAIL"}, headers=alice).status_code == 409
    assert client.post(f"{url}/entities", json={"start": 0, "end": 4, "entity_type": "PERSON"}, headers=alice).status_code == 409


# ---------------------------------------------------------------------------- concurrency and cleanup
def test_concurrent_decisions_keep_history_consistent(client, alice, scan):
    e = first(client, alice, scan["id"], "PHONE")
    url = f"/api/v1/entities/{e['id']}/review"
    actions = ["REDACT", "KEEP"] * 8
    codes = []

    def go(a):
        codes.append(client.post(url, json={"action": a}, headers=alice).status_code)

    threads = [threading.Thread(target=go, args=(a,)) for a in actions]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [200] * len(actions)
    hist = client.get(f"/api/v1/entities/{e['id']}/history", headers=alice).json()
    assert len(hist) == len(actions)
    cur = first(client, alice, scan["id"], "PHONE")["review_status"]
    assert cur == hist[-1]["action"]  # current status always equals the newest history row


def test_review_data_is_removed_with_the_document(client, alice, rita, scan):
    client.post(f"{DOCS}/{scan['doc']}/reviewers", json={"email": "rita@example.com"}, headers=alice)
    e = first(client, alice, scan["id"], "EMAIL")
    client.post(f"/api/v1/entities/{e['id']}/review", json={"action": "REDACT"}, headers=alice)
    assert client.delete(f"{DOCS}/{scan['doc']}", headers=alice).status_code == 204
    with SessionLocal() as db:
        from app.models import DocumentReviewer

        assert db.scalar(select(func.count()).select_from(RedactionAction)) == 0
        assert db.scalar(select(func.count()).select_from(DocumentReviewer)) == 0
    assert client.get("/api/v1/review/assigned", headers=rita).json()["total"] == 0
