"""Create demo users (fictional). Usage: python -m scripts.seed

Passwords come from SEED_PASSWORD (env) or are generated and printed once.
"""
import os
import secrets
import sys

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import User
from app.security.passwords import hash_password

DEMO_USERS = [
    ("Demo Admin", "admin@example.com", "ADMIN"),
    ("Demo Reviewer", "reviewer@example.com", "REVIEWER"),
    ("Demo User", "user@example.com", "USER"),
]


def main() -> None:
    from app.core.config import get_settings  # imported here to avoid import before env is loaded
    if get_settings().environment != "development":
        print("ERROR: seed.py must not be run outside ENVIRONMENT=development (gap 6)")
        sys.exit(1)
    password = os.environ.get("SEED_PASSWORD") or secrets.token_urlsafe(12)
    generated = "SEED_PASSWORD" not in os.environ
    with SessionLocal() as db:
        created = []
        for name, email, role in DEMO_USERS:
            if db.scalar(select(User).where(User.email == email)):
                continue
            db.add(User(name=name, email=email, role=role, password_hash=hash_password(password)))
            created.append(email)
        db.commit()
    if created:
        print("created:", ", ".join(created))
        if generated:
            print("password (shown once):", password)
    else:
        print("demo users already exist")


if __name__ == "__main__":
    main()
