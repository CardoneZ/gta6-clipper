"""
Credentials with the standard library only: scrypt password hashes, HMAC-signed session tokens,
hashed API keys. No secret is ever stored or logged in clear text.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
SESSION_TTL_SEC = 7 * 24 * 3600
API_KEY_PREFIX = "gtc_"
API_KEY_LOOKUP_CHARS = 12  # "gtc_" + 8 random chars: indexed lookup without storing the key
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
MIN_PASSWORD_LEN = 10
MAX_PASSWORD_LEN = 256


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def normalize_email(email: Any) -> str | None:
    value = str(email or "").strip().lower()
    return value if EMAIL_RE.match(value) else None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        test = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(_unb64(dk)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(test, _unb64(dk))


def sign_session(user_id: str, secret: str, ttl_sec: int = SESSION_TTL_SEC, now: float | None = None) -> str:
    payload = {"sub": user_id, "exp": int((now or time.time()) + ttl_sec)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_session(token: str, secret: str, now: float | None = None) -> str | None:
    """User id from a valid, unexpired session token; None otherwise."""
    try:
        body, sig = token.split(".", 1)
        want = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("sub"), str) or not isinstance(payload.get("exp"), int):
        return None
    if payload["exp"] < (now or time.time()):
        return None
    return payload["sub"]


def new_api_key() -> tuple[str, str, str]:
    """(full key shown once, lookup prefix, sha256 hex stored)."""
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[:API_KEY_LOOKUP_CHARS], hashlib.sha256(key.encode("utf-8")).hexdigest()


def api_key_matches(key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(key.encode("utf-8")).hexdigest(), stored_hash)
