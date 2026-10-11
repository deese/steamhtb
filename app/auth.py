import base64
import hashlib
import hmac
import time

from .config import LOGIN_PASSWORD, SESSION_DAYS

COOKIE_NAME = "steamhtb_session"


def _signing_key() -> bytes:
    # Derived from the password itself rather than a separate secret: keeps
    # setup to a single env var, and changing the password naturally
    # invalidates every existing session cookie.
    return hashlib.pbkdf2_hmac("sha256", LOGIN_PASSWORD.encode(), b"steamhtb-cookie-salt", 100_000)


def create_session_cookie() -> str:
    expiry = int(time.time()) + SESSION_DAYS * 86400
    payload = str(expiry).encode()
    sig = hmac.new(_signing_key(), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload).decode() + "." + sig


def verify_session_cookie(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload_b64, _, sig = token.rpartition(".")
    try:
        payload = base64.urlsafe_b64decode(payload_b64.encode())
        expiry = int(payload.decode())
    except Exception:
        return False
    expected_sig = hmac.new(_signing_key(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False
    return time.time() < expiry


def verify_password(password: str) -> bool:
    return bool(LOGIN_PASSWORD) and hmac.compare_digest(password, LOGIN_PASSWORD)
