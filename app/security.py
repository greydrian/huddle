"""
Admin PIN hashing/verification, session tokens, and encryption for stored
OAuth tokens.

Uses stdlib PBKDF2 (no extra dependency) for PIN hashing, and itsdangerous
for short-lived, signed session cookies. Failed attempts trigger exponential
backoff, per the spec's admin hardening requirements.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from itertools import pairwise

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.database import DATA_DIR

SECRET_KEY_PATH = DATA_DIR / ".secret_key"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 2  # 2 hour admin session
BASE_LOCKOUT_SECONDS = 5  # doubles per consecutive failure
MAX_BACKOFF_SECONDS = 300
# Sustained guessing: from this many consecutive failures, a long lockout.
LONG_LOCKOUT_AFTER = 10
LONG_LOCKOUT_SECONDS = 15 * 60
# Failures this far apart don't add up: the count starts again, so a stray
# typo weeks later isn't treated as sustained guessing.
FAILURE_DECAY_SECONDS = 24 * 60 * 60
DEFAULT_PIN = "1234"  # seeded on a fresh install; Admin forces a change
# Baked into every stored PIN hash — changing it invalidates existing PINs.
PBKDF2_ITERATIONS = 200_000


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
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:  # a corrupt stored hash reads as "no match", not a crash
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(candidate.hex(), hash_hex)


def lockout_seconds_for(failed_attempts: int) -> int:
    """Exponential backoff: 5s, 10s, 20s, 40s ... capped at 5 minutes, then
    15 minutes per attempt once LONG_LOCKOUT_AFTER failures have piled up."""
    if failed_attempts <= 0:
        return 0
    if failed_attempts >= LONG_LOCKOUT_AFTER:
        return LONG_LOCKOUT_SECONDS
    seconds = BASE_LOCKOUT_SECONDS * (2 ** (failed_attempts - 1))
    return min(seconds, MAX_BACKOFF_SECONDS)


def is_weak_pin(pin: str) -> bool:
    """One digit repeated (0000, 1111) or a straight run up or down (1234,
    9876, 012345) — the first things anyone would try."""
    if len(set(pin)) == 1:
        return True
    steps = {int(b) - int(a) for a, b in pairwise(pin)}
    return steps in ({1}, {-1})


def create_session_token(generation: int = 0) -> str:
    """`generation` is the session generation (see auth.py) at login;
    logging out or changing the PIN bumps it, killing older tokens."""
    serializer = URLSafeTimedSerializer(_get_secret_key())
    return serializer.dumps({"admin": True, "gen": generation, "issued_at": time.time()})


def verify_session_token(token: str | None, generation: int = 0) -> bool:
    if not token:
        return False
    serializer = URLSafeTimedSerializer(_get_secret_key())
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except BadSignature, SignatureExpired:
        return False
    # Tokens issued before generations existed carry none: treat as 0.
    return bool(data.get("admin")) and data.get("gen", 0) == generation


def _get_fernet() -> Fernet:
    """Symmetric key for encrypting stored OAuth tokens at rest, derived
    from the same local secret file the session-cookie signer uses —
    one piece of key material for the whole app, not two to manage."""
    return Fernet(base64.urlsafe_b64encode(_get_secret_key()))


def encrypt_token_json(data: dict) -> str:
    return _get_fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_token_json(encrypted: str) -> dict | None:
    """Returns None on a bad/foreign token rather than raising — a token
    stored under an old secret key (or corrupted) should read back as
    'not connected', not crash the dashboard."""
    try:
        return json.loads(_get_fernet().decrypt(encrypted.encode()))
    except InvalidToken, ValueError:
        return None
