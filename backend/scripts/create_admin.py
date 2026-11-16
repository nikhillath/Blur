"""Create an administrator (or promote/reset an existing account).

    set ADMIN_EMAIL=you@yourdomain.com
    set ADMIN_PASSWORD=<at least 10 characters>
    set ADMIN_NAME="Your Name"          (optional)
    python -m scripts.create_admin

The password is read from the environment so it never appears in shell history or process listings.
"""
import os
import sys

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import User
from app.security.passwords import hash_password


def main() -> int:
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if "@" not in email or len(password) < 10:
        print("set ADMIN_EMAIL and ADMIN_PASSWORD (at least 10 characters)", file=sys.stderr)
        return 2
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            db.add(User(name=os.environ.get("ADMIN_NAME", "Administrator"), email=email, role="ADMIN",
                        password_hash=hash_password(password)))
            action = "created"
        else:
            user.role, user.is_active, user.password_hash = "ADMIN", True, hash_password(password)
            action = "updated (role set to ADMIN, password reset, account enabled)"
        db.commit()
    print(f"admin {email} {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
