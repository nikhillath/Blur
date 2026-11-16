import time

import jwt
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import AuditLog, RefreshToken, User
from tests.conftest import PASSWORD

REG = {"name": "Riya Demo", "email": "Riya.Demo@Example.com", "password": PASSWORD}


def test_register_login_me(client):
    r = client.post("/api/v1/auth/register", json=REG)
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "riya.demo@example.com"
    assert body["role"] == "USER"
    assert "password" not in str(body)

    r = client.post("/api/v1/auth/login", json={"email": "riya.demo@example.com", "password": PASSWORD})
    assert r.status_code == 200
    tok = r.json()["access_token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert me.status_code == 200 and me.json()["email"] == "riya.demo@example.com"


def test_password_stored_as_argon2id(client):
    client.post("/api/v1/auth/register", json=REG)
    with SessionLocal() as db:
        u = db.scalar(select(User))
        assert u.password_hash.startswith("$argon2id$")
        assert PASSWORD not in u.password_hash


def test_register_cannot_choose_role(client):
    r = client.post("/api/v1/auth/register", json={**REG, "role": "ADMIN"})
    assert r.status_code == 201 and r.json()["role"] == "USER"


def test_duplicate_email_conflict(client):
    assert client.post("/api/v1/auth/register", json=REG).status_code == 201
    assert client.post("/api/v1/auth/register", json={**REG, "email": "riya.demo@example.com"}).status_code == 409


def test_register_validation(client):
    assert client.post("/api/v1/auth/register", json={**REG, "password": "short"}).status_code == 422
    assert client.post("/api/v1/auth/register", json={**REG, "email": "not-an-email"}).status_code == 422


def test_login_wrong_password_and_unknown_user_same_error(client, make_user):
    make_user("a@example.com")
    r1 = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "wrong-password-x"})
    r2 = client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong-password-x"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json()


def test_disabled_user_cannot_login_or_use_token(client, make_user, auth_headers):
    make_user("a@example.com")
    hdr = auth_headers("a@example.com")
    with SessionLocal() as db:
        u = db.scalar(select(User))
        u.is_active = False
        db.commit()
    assert client.get("/api/v1/auth/me", headers=hdr).status_code == 401
    r = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 401


def test_unauthenticated_and_bad_tokens(client):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_expired_and_forged_tokens_rejected(client, make_user):
    u = make_user("a@example.com")
    key = get_settings().secret_key
    base = {"sub": str(u.id), "role": "ADMIN", "type": "access", "sid": str(u.id)}
    expired = jwt.encode({**base, "exp": int(time.time()) - 10}, key, algorithm="HS256")
    forged = jwt.encode({**base, "exp": int(time.time()) + 600}, "wrong-key-wrong-key-wrong-key-123", algorithm="HS256")
    refresh_typed = jwt.encode({**base, "type": "refresh", "exp": int(time.time()) + 600}, key, algorithm="HS256")
    for t in (expired, forged, refresh_typed):
        assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_token_role_claim_is_not_trusted(client, make_user, auth_headers):
    """A validly signed token claiming role=ADMIN must not grant admin: role comes from the DB."""
    u = make_user("a@example.com", role="USER")
    auth_headers("a@example.com")  # creates a real session
    with SessionLocal() as db:
        sid = db.scalar(select(RefreshToken)).family_id
    t = jwt.encode(
        {"sub": str(u.id), "role": "ADMIN", "type": "access", "sid": str(sid), "exp": int(time.time()) + 600},
        get_settings().secret_key, algorithm="HS256",
    )
    assert client.get("/api/v1/users", headers={"Authorization": f"Bearer {t}"}).status_code == 403


def _login(client, email="a@example.com"):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, client.cookies.get("pii_refresh")


def _use_cookie(client, raw):
    client.cookies.clear()
    client.cookies.set("pii_refresh", raw, path="/api/v1/auth")


def test_refresh_rotates_token_and_access_token_survives(client, make_user):
    make_user("a@example.com")
    hdr, old = _login(client)
    r = client.post("/api/v1/auth/refresh")
    assert r.status_code == 200
    new = client.cookies.get("pii_refresh")
    assert new and new != old
    # the access token issued BEFORE the refresh must keep working (in-flight requests, other tabs)
    assert client.get("/api/v1/auth/me", headers=hdr).status_code == 200
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).status_code == 200


def test_replay_inside_grace_is_rejected_but_does_not_end_the_session(client, make_user):
    make_user("a@example.com")
    hdr, old = _login(client)
    client.post("/api/v1/auth/refresh")
    new = client.cookies.get("pii_refresh")
    _use_cookie(client, old)                                    # a second tab still holding the old cookie
    assert client.post("/api/v1/auth/refresh").status_code == 401
    _use_cookie(client, new)                                    # the session itself is fine
    assert client.post("/api/v1/auth/refresh").status_code == 200
    assert client.get("/api/v1/auth/me", headers=hdr).status_code == 200


def test_replay_after_grace_ends_only_that_login(client, make_user):
    from datetime import timedelta

    make_user("a@example.com")
    hdr_phone, old = _login(client)
    client.post("/api/v1/auth/refresh")
    new = client.cookies.get("pii_refresh")
    hdr_laptop, laptop_cookie = _login(client)                  # a second, independent login
    with SessionLocal() as db:                                  # pretend the rotation happened a minute ago
        for t in db.scalars(select(RefreshToken).where(RefreshToken.revoked_at.is_not(None))):
            t.revoked_at = t.revoked_at - timedelta(minutes=1)
        db.commit()
    _use_cookie(client, old)
    assert client.post("/api/v1/auth/refresh").status_code == 401   # stolen-token signal
    _use_cookie(client, new)
    assert client.post("/api/v1/auth/refresh").status_code == 401   # that login family is dead
    assert client.get("/api/v1/auth/me", headers=hdr_phone).status_code == 401
    assert client.get("/api/v1/auth/me", headers=hdr_laptop).status_code == 200   # the other login is untouched
    _use_cookie(client, laptop_cookie)
    assert client.post("/api/v1/auth/refresh").status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "REFRESH_REUSE_DETECTED"))


def test_refresh_token_stored_hashed(client, make_user):
    make_user("a@example.com")
    client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    raw = client.cookies.get("pii_refresh")
    with SessionLocal() as db:
        row = db.scalar(select(RefreshToken))
        assert row.token_hash != raw and len(row.token_hash) == 64


def test_logout_revokes_refresh(client, make_user):
    make_user("a@example.com")
    client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    raw = client.cookies.get("pii_refresh")
    assert client.post("/api/v1/auth/logout").status_code == 204
    client.cookies.clear(); client.cookies.set("pii_refresh", raw, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_refresh_without_cookie(client):
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_audit_log_has_no_secrets(client, make_user):
    make_user("a@example.com")
    client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "bad-password-xx"})
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog)).all()
        actions = {r.action for r in rows}
        assert {"LOGIN_SUCCEEDED", "LOGIN_FAILED"} <= actions
        assert PASSWORD not in str([r.details for r in rows])


def test_access_token_dies_on_logout(client, make_user):
    make_user("a@example.com")
    r = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    hdr = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=hdr).status_code == 200
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/auth/me", headers=hdr).status_code == 401


def test_access_token_without_session_rejected(client, make_user):
    u = make_user("a@example.com")
    t = jwt.encode({"sub": str(u.id), "type": "access", "exp": int(time.time()) + 600},
                   get_settings().secret_key, algorithm="HS256")
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401
