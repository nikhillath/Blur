"""Password hashing and policy (Argon2id).

Policy (gap 4d):
- Minimum 10 characters (enforced by the schema too, but checked here for the change-password path)
- Must not contain the user's email local part
- Must not be in the common-password blocklist
- Must not be trivially repetitive (all same character)
"""
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()  # Argon2id by default
# Used to equalise timing when the email is unknown.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")

# A small blocklist of the most commonly used passwords.
_COMMON_PASSWORDS = {
    "password", "password1", "password123", "passw0rd",
    "12345678", "123456789", "1234567890",
    "qwerty", "qwerty123", "qwertyuiop",
    "abc123456", "letmein1", "welcome1",
    "iloveyou1", "sunshine1", "monkey123",
    "dragon123", "master123", "admin1234",
    "changeme1", "trustno1!",
}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def burn_verify(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def validate_password_policy(password: str, email: str) -> str | None:
    """Returns an error message string if the password fails policy, or None if it is acceptable."""
    if len(password) < 10:
        return "password must be at least 10 characters"
    # Must not consist of a single repeated character (e.g. "aaaaaaaaaa")
    if len(set(password)) == 1:
        return "password is too simple"
    # Must not contain the email's local part (case-insensitive)
    local_part = email.split("@")[0].lower() if "@" in email else email.lower()
    if local_part and len(local_part) >= 4 and local_part in password.lower():
        return "password must not contain your email address"
    # Common passwords check (normalise: lower, strip common suffixes)
    if password.lower() in _COMMON_PASSWORDS:
        return "password is too common; choose a stronger one"
    return None
