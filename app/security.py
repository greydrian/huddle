"""
Admin PIN hashing/verification and session tokens.

Uses stdlib PBKDF2 (no extra dependency) for PIN hashing, and itsdangerous
for short-lived, signed session cookies. Failed attempts trigger exponential
backoff, per the spec's admin hardening requirements.
"""

import hashlib
import hmac
import os
import time
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.database import DATA_DIR

SECRET_KEY_PATH = DATA_DIR / ".secret_key"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 2  # 2 hour admin session
BASE_LOCKOUT_SECONDS = 5  # doubles per consecutive failure


def _get_secret_key() -> bytes:
    """Load (or generate once) a local secret key used to sign session cookies."""
    path = SECRET_KEY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(os.urandom(32))
    return path.read_bytes()


def hash_pin(pin: str, salt: bytes | None = None) -> str:
    """Return 'salt_hex$hash_hex' using PBKDF2-HMAC-SHA256."""
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    candidate = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000)
    return hmac.compare_digest(candidate.hex(), hash_hex)


def lockout_seconds_for(failed_attempts: int) -> int:
    """Exponential backoff: 5s, 10s, 20s, 40s ... capped at 5 minutes."""
    if failed_attempts <= 0:
        return 0
    seconds = BASE_LOCKOUT_SECONDS * (2 ** (failed_attempts - 1))
    return min(seconds, 300)


def create_session_token() -> str:
    serializer = URLSafeTimedSerializer(_get_secret_key())
    return serializer.dumps({"admin": True, "issued_at": time.time()})


def verify_session_token(token: str | None) -> bool:
    if not token:
        return False
    serializer = URLSafeTimedSerializer(_get_secret_key())
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
        return bool(data.get("admin"))
    except (BadSignature, SignatureExpired):
        return False
