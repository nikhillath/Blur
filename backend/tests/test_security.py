import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.ratelimit import limiter
from app.db.session import SessionLocal
from app.main import app, create_app
from app.models import Document, PiiEntity
from tests.conftest import PASSWORD
from tests.test_detection_pipeline import FULL

DOCS = "/api/v1/documents"
PUBLIC = {"/health/live", "/health/ready", "/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/refresh",
          "/api/v1/auth/logout", "/api/v1/auth/config", "/openapi.json", "/docs", "/docs/oauth2-redirect"}
ADMIN_ONLY = {"/api/v1/users", "/api/v1/users/{user_id}", "/api/v1/audit-logs"}
# Valid bodies, so a request reaches the ownership check instead of failing validation first.
BODIES = {
    ("POST", "/api/v1/entities/{entity_id}/review"): {"action": "KEEP"},
    ("PATCH", "/api/v1/entities/{entity_id}"): {"entity_type": "PHONE"},
    ("POST", "/api/v1/scans/{scan_id}/reviews/bulk"): {"action": "KEEP", "entity_type": "EMAIL"},
    ("POST", "/api/v1/scans/{scan_id}/entities"): {"start": 0, "end": 4, "entity_type": "PERSON"},
    ("POST", "/api/v1/documents/{document_id}/reviewers"): {"email": "rita@example.com"},
    ("POST", "/api/v1/scans/{scan_id}/redact"): {"mask_style": "full"},
}


def routes():
    """Every (METHOD, path) the app exposes, read from its OpenAPI schema so new routes are picked up automatically."""
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            if method.upper() in ("GET", "POST", "PATCH", "PUT", "DELETE"):
                yield method.upper(), path


def fill(path, ids):
    return re.sub(r"\{(\w+)\}", lambda m: ids.get(m.group(1), str(uuid.uuid4())), path)


@pytest.fixture
def world(client, make_user, auth_headers):
    """Alice owns a scanned, partly-reviewed, redacted document. Others try to touch it."""
    make_user("alice@example.com")
    make_user("bob@example.com")
    make_user("rita@example.com", "REVIEWER")
    make_user("admin@example.com", "ADMIN")
    a = auth_headers("alice@example.com")
    did = client.post(DOCS, headers=a, files={"file": ("d.txt", FULL.encode(), "text/plain")}).json()["id"]
    sid = client.post(f"{DOCS}/{did}/scan", headers=a).json()["id"]
    eid = client.get(f"/api/v1/scans/{sid}/entities", headers=a).json()["items"][0]["id"]
    client.post(f"/api/v1/scans/{sid}/reviews/bulk", json={"action": "REDACT", "entity_type": "EMAIL"}, headers=a)
    rid = client.post(f"/api/v1/scans/{sid}/redact", headers=a).json()["id"]
    others = {n: auth_headers(f"{n}@example.com") for n in ("bob", "rita", "admin")}
    return {"a": a, "others": others, "ids": {"document_id": did, "scan_id": sid, "entity_id": eid, "redacted_id": rid}}


# ------------------------------------------------------------------------------ authentication coverage
def test_every_route_requires_authentication_except_the_public_ones(client):
    checked = 0
    for method, path in routes():
        if path in PUBLIC:
            continue
        r = client.request(method, fill(path, {}), json=BODIES.get((method, path)))
        assert r.status_code == 401, f"{method} {path} answered {r.status_code} without a token"
        checked += 1
    assert checked >= 28  # the matrix really enumerated the API (31 at the time of writing)


def test_garbage_and_wrong_scheme_tokens_are_rejected(client):
    for h in ({"Authorization": "Bearer"}, {"Authorization": "Basic YTpi"}, {"Authorization": "Bearer a.b.c"}, {"Authorization": "bearer " + "x" * 5000}):
        assert client.get("/api/v1/auth/me", headers=h).status_code == 401


# ------------------------------------------------------------------------------ authorisation matrix (IDOR)
def test_no_other_user_can_reach_alices_resources_through_any_route(client, world):
    ids, tried = world["ids"], 0
    for who, hdr in world["others"].items():
        for method, path in routes():
            if path in PUBLIC or path in ADMIN_ONLY or not re.search(r"\{(document|scan|entity|redacted)_id\}", path):
                continue
            body = BODIES.get((method, path))
            if method in ("POST", "PATCH") and "{reviewer_id}" not in path and body is None and not path.endswith(("/scan", "/retry")):
                pytest.fail(f"add a valid body for {method} {path} to BODIES")
            r = client.request(method, fill(path, ids), headers=hdr, json=body)
            assert r.status_code in (404, 403), f"{who} got {r.status_code} from {method} {path}"
            tried += 1
    assert tried > 60
    a = world["a"]  # nothing of Alice's was changed, deleted or leaked by the attempts
    assert client.get(f"{DOCS}/{ids['document_id']}", headers=a).status_code == 200
    assert client.get(f"/api/v1/redacted-documents/{ids['redacted_id']}", headers=a).status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(PiiEntity).where(PiiEntity.id == uuid.UUID(ids["entity_id"]))).review_status in ("PENDING", "REDACT")
        assert db.scalar(select(Document).where(Document.id == uuid.UUID(ids["document_id"])))


def test_list_endpoints_never_include_other_users_data(client, world):
    for hdr in world["others"].values():
        assert client.get(DOCS, headers=hdr).json()["total"] == 0
        assert client.get("/api/v1/review/assigned", headers=hdr).json()["total"] == 0
        assert client.get("/api/v1/dashboard", headers=hdr).json()["documents"] == 0


def test_admin_only_routes_forbidden_for_everyone_else(client, world):
    for who in ("bob", "rita"):
        for method, path in routes():
            if path in ADMIN_ONLY:
                r = client.request(method, fill(path, {}), headers=world["others"][who], json={"role": "USER"} if method == "PATCH" else None)
                assert r.status_code == 403, f"{who} {method} {path} -> {r.status_code}"
    assert client.get("/api/v1/users", headers=world["others"]["admin"]).status_code == 200


# ------------------------------------------------------------------------------ input handling and error hygiene
def test_validation_errors_do_not_echo_submitted_secrets(client):
    r = client.post("/api/v1/auth/register", json={"name": "A", "email": "a@example.com", "password": "short-pw"})
    assert r.status_code == 422 and "short-pw" not in r.text and "input" not in r.text
    r = client.post("/api/v1/auth/login", json={"email": "not-an-email", "password": "hunter2hunter2"})
    assert r.status_code == 422 and "hunter2hunter2" not in r.text


@pytest.mark.parametrize("payload", ["' OR '1'='1", "1; DROP TABLE users; --", "%' OR 1=1 --", "\\", "%_%", "\x00", "𝕏" * 90])
def test_injection_style_input_is_inert(client, world, payload):
    a = world["a"]
    for r in (client.get(DOCS, params={"q": payload}, headers=a),
              client.get(f"/api/v1/scans/{world['ids']['scan_id']}/entities", params={"entity_type": payload}, headers=a)):
        assert r.status_code in (200, 422)
    assert client.get(DOCS, headers=a).json()["total"] == 1  # nothing was dropped or widened


def test_unhandled_errors_are_sanitised(world, monkeypatch):
    from app.api.v1 import dashboard

    monkeypatch.setattr(dashboard, "_PENDING", dashboard.text("SELECT * FROM table_that_does_not_exist"))
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/v1/dashboard", headers=world["a"])
    assert r.status_code == 500 and r.json() == {"detail": "internal server error"}
    assert "table_that_does_not_exist" not in r.text and "Traceback" not in r.text and "psycopg" not in r.text


def test_chunked_bodies_without_length_are_refused(client, world):
    r = client.post("/api/v1/auth/login", content=(b"x" for _ in range(3)), headers={"Content-Type": "application/json"})
    assert r.status_code == 411


# ------------------------------------------------------------------------------ transport / browser protections
def test_security_headers_present(client):
    h = client.get("/health/live").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"


def test_production_mode_hides_docs_and_locks_down_responses(monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "production")
    with TestClient(create_app()) as c:
        assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
        h = c.get("/health/live").headers
        assert "default-src 'none'" in h["content-security-policy"] and "max-age" in h["strict-transport-security"]


def test_cors_allows_only_configured_origin_and_methods(client):
    ok = client.options("/api/v1/auth/login", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    evil = client.options("/api/v1/auth/login", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in evil.headers
    bad = client.options("/api/v1/auth/login", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "TRACE"})
    assert bad.status_code == 400


def test_cookie_endpoints_reject_foreign_origins(client, make_user):
    make_user("a@example.com")
    client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert client.post("/api/v1/auth/refresh", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/v1/auth/logout", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/v1/auth/refresh", headers={"Origin": "http://localhost:5173"}).status_code == 200
    assert client.post("/api/v1/auth/refresh").status_code == 200   # non-browser clients send no Origin


def test_refresh_cookie_flags(client, make_user, monkeypatch):
    make_user("a@example.com")
    c = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD}).headers["set-cookie"].lower()
    assert "httponly" in c and "samesite=lax" in c and "path=/api/v1/auth" in c and "secure" not in c
    monkeypatch.setattr(get_settings(), "environment", "production")
    c = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD}).headers["set-cookie"].lower()
    assert "secure" in c   # sent only over HTTPS outside development


def test_access_token_is_not_stored_in_a_cookie_or_response_header(client, make_user):
    make_user("a@example.com")
    r = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    token = r.json()["access_token"]
    assert token not in r.headers.get("set-cookie", "") and token not in str(dict(r.headers))


# ------------------------------------------------------------------------------ configuration and abuse limits
def test_weak_secrets_refused_outside_development():
    Settings(environment="development", secret_key="change-me-in-env")        # allowed locally
    for bad in ("change-me-in-env", "too-short"):
        with pytest.raises(ValueError, match="SECRET_KEY"):
            Settings(environment="production", secret_key=bad)
    Settings(environment="production", secret_key="x" * 40)


def test_login_is_rate_limited(client, make_user):
    make_user("a@example.com")
    limiter.enabled = True
    limiter.reset()
    try:
        codes = [client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "wrong-password-1"}).status_code for _ in range(14)]
    finally:
        limiter.enabled = False
        limiter.reset()
    assert codes[:10] == [401] * 10 and 429 in codes[10:]


def test_upload_and_redact_have_a_rate_limit(client, world):
    limiter.enabled = True
    limiter.reset()
    try:
        codes = [client.post(DOCS, headers=world["a"], files={"file": ("x.txt", b"hello world", "text/plain")}).status_code for _ in range(64)]
    finally:
        limiter.enabled = False
        limiter.reset()
    assert 429 in codes and codes.count(201) <= 60


def test_sensitive_values_never_appear_in_logs(client, world, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        client.get(f"/api/v1/scans/{world['ids']['scan_id']}/text", headers=world["a"])
        client.post("/api/v1/auth/login", json={"email": "alice@example.com", "password": "definitely-wrong"})
    logged = caplog.text
    assert "riya.sharma@example.com" not in logged and "definitely-wrong" not in logged and "Bearer " not in logged
