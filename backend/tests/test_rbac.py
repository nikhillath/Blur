from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import AuditLog, User


def test_users_endpoint_forbidden_for_non_admin(client, make_user, auth_headers):
    make_user("u@example.com", "USER")
    make_user("r@example.com", "REVIEWER")
    assert client.get("/api/v1/users").status_code == 401
    for e in ("u@example.com", "r@example.com"):
        assert client.get("/api/v1/users", headers=auth_headers(e)).status_code == 403
        assert client.get("/api/v1/audit-logs", headers=auth_headers(e)).status_code == 403


def test_admin_lists_users_with_pagination(client, make_user, auth_headers):
    make_user("admin@example.com", "ADMIN")
    for i in range(3):
        make_user(f"u{i}@example.com")
    r = client.get("/api/v1/users?page=1&page_size=2", headers=auth_headers("admin@example.com"))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4 and len(body["items"]) == 2
    assert "password_hash" not in str(body)


def test_admin_changes_role_and_audits(client, make_user, auth_headers):
    make_user("admin@example.com", "ADMIN")
    target = make_user("u@example.com")
    hdr = auth_headers("admin@example.com")
    r = client.patch(f"/api/v1/users/{target.id}", json={"role": "REVIEWER"}, headers=hdr)
    assert r.status_code == 200 and r.json()["role"] == "REVIEWER"
    with SessionLocal() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "ADMIN_USER_UPDATED"))
    logs = client.get("/api/v1/audit-logs?action=ADMIN_USER_UPDATED", headers=hdr).json()
    assert logs["total"] == 1


def test_admin_cannot_demote_or_disable_self(client, make_user, auth_headers):
    admin = make_user("admin@example.com", "ADMIN")
    hdr = auth_headers("admin@example.com")
    assert client.patch(f"/api/v1/users/{admin.id}", json={"role": "USER"}, headers=hdr).status_code == 400
    assert client.patch(f"/api/v1/users/{admin.id}", json={"is_active": False}, headers=hdr).status_code == 400


def test_disabling_user_revokes_sessions(client, make_user, auth_headers):
    make_user("admin@example.com", "ADMIN")
    target = make_user("u@example.com")
    auth_headers("u@example.com")  # creates a refresh session
    user_session_cookie = client.cookies.get("pii_refresh")
    hdr = auth_headers("admin@example.com")
    client.patch(f"/api/v1/users/{target.id}", json={"is_active": False}, headers=hdr)
    client.cookies.clear(); client.cookies.set("pii_refresh", user_session_cookie, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401
    with SessionLocal() as db:
        assert db.scalar(select(User).where(User.id == target.id)).is_active is False


def test_invalid_role_rejected(client, make_user, auth_headers):
    make_user("admin@example.com", "ADMIN")
    t = make_user("u@example.com")
    r = client.patch(f"/api/v1/users/{t.id}", json={"role": "ROOT"}, headers=auth_headers("admin@example.com"))
    assert r.status_code == 422


def test_unknown_user_404(client, make_user, auth_headers):
    make_user("admin@example.com", "ADMIN")
    r = client.patch(
        "/api/v1/users/00000000-0000-0000-0000-000000000000",
        json={"role": "USER"}, headers=auth_headers("admin@example.com"),
    )
    assert r.status_code == 404
