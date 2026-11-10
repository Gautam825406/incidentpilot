"""Auth (demo-grade JWT + PBKDF2 hashing) and guardrails.

Demo auth only: the JWT is HMAC-SHA256 via the standard library, passwords
are PBKDF2-HMAC-SHA256. This is honest for a portfolio demo — it is NOT
production auth (use a proper identity provider / bcrypt + rotation there).
Prompt-injection guard: log/document text is data, never instructions.
"""
import base64
import hashlib
import hmac
import json
import re
import time
from typing import Optional

from .config import settings

_INJECTION_PATTERNS = [
    r"ignore (all )?(previous|above) instructions",
    r"disregard (all )?(previous|above) instructions",
    r"you are now ",
    r"system prompt",
    r"execute .* (shell|command|rm |delete)",
    r"reveal (the )?(api key|secret|password)",
]


def hash_password(password: str, salt: str = "incidentpilot-demo-salt") -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def verify_password(password: str, password_hash: str) -> bool:
    return hmac.compare_digest(hash_password(password), password_hash)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def create_token(user_id: str, email: str, role: str, expires_in: int = 86400) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(
        json.dumps({"sub": user_id, "email": email, "role": role, "exp": int(time.time()) + expires_in}).encode()
    )
    sig = _b64(hmac.new(settings.jwt_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def verify_token(token: str) -> Optional[dict]:
    try:
        header, payload, sig = token.split(".")
        expected = _b64(hmac.new(settings.jwt_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def redact_pii(text: str) -> str:
    """Redact emails, bearer tokens, and long hex secrets from log text."""
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]{10,}", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)[A-Za-z0-9\-_]{8,}", r"\1[REDACTED]", text)
    return text


def contains_injection(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(p, lowered) for p in _INJECTION_PATTERNS)


def sanitise_evidence(text: str) -> str:
    """Treat untrusted text as data: redact PII and flag injection attempts."""
    clean = redact_pii(text)
    if contains_injection(text):
        return clean + "\n[FLAGGED: instruction-like content in log data — treated as data, not instructions]"
    return clean
