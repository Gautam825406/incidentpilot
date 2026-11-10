"""Auth (demo-grade JWT + PBKDF2 hashing) and guardrails.

Demo auth only: the JWT is HMAC-SHA256 via the standard library, passwords
are PBKDF2-HMAC-SHA256. This is honest for a portfolio demo — it is NOT
production auth (use a proper identity provider / bcrypt + rotation there).
Prompt-injection guard: log/document text is data, never instructions.

Password salts (Phase 4): NEW users get a per-user random salt, stored
alongside the hash as ``"<hex-salt>$<hex-hash>"``. The original demo users
were seeded with one FIXED salt and no salt in the stored value; those
legacy hashes keep verifying (see _LEGACY_SALT) so existing demo logins
(admin@demo.local/admin123 etc.) keep working without a password reset.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Optional

from .config import settings

# Fixed salt used by the original demo seed (pre–Phase 4). Kept only so
# legacy hashes still verify. Never used for new passwords.
_LEGACY_SALT = "incidentpilot-demo-salt"

_INJECTION_PATTERNS = [
    r"ignore (all )?(previous|above) instructions",
    r"disregard (all )?(previous|above) instructions",
    r"you are now ",
    r"system prompt",
    r"execute .* (shell|command|rm |delete)",
    r"reveal (the )?(api key|secret|password)",
]


def hash_password(password: str, salt: str | None = None) -> str:
    """Hash a password with a per-user random salt.

    Returns ``"<salt>$<hash>"`` (both hex). Pass an explicit ``salt`` only
    for deterministic tests; production callers leave it unset so a fresh
    random salt is generated every time.
    """
    if salt is None:
        salt = secrets.token_hex(16)  # 128-bit random salt per user
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify against both formats: new ``"salt$hash"`` and legacy
    fixed-salt bare hex hashes (seeded demo users)."""
    if "$" in password_hash:
        salt, expected = password_hash.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
        return hmac.compare_digest(digest, expected)
    # Legacy: bare hex hash produced with the old fixed salt.
    legacy = hashlib.pbkdf2_hmac("sha256", password.encode(), _LEGACY_SALT.encode(), 100_000).hex()
    return hmac.compare_digest(legacy, password_hash)


def is_legacy_hash(password_hash: str) -> bool:
    """True when ``password_hash`` is a pre–Phase 4 bare hex hash (fixed
    salt, no ``"$"`` separator). Legacy hashes still verify, but a
    successful login upgrades them to a per-user random salt (see
    main.py login) so the fixed salt disappears over time. The legacy
    users are the public demo accounts (admin123 etc. are in the README),
    so the fixed salt was never a secret — but one shared salt for every
    user is still weaker than per-user salts, hence the upgrade."""
    return "$" not in password_hash


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def create_token(user_id: str, email: str, role: str, expires_in: int = 86400) -> str:
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(
        json.dumps({"sub": user_id, "email": email, "role": role, "iat": now, "exp": now + expires_in}).encode()
    )
    sig = _b64(hmac.new(settings.jwt_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def verify_token(token: str) -> Optional[dict]:
    """Verify a JWT. Defence-in-depth beyond the HMAC (the signature
    already covers header+payload, so a forged header cannot pass
    without the secret): the header must declare exactly alg HS256 /
    typ JWT (algorithm-confusion defence — a token claiming ``alg:
    none`` or an asymmetric alg is rejected before the payload is
    trusted), and the payload must carry a string sub/email/role and
    an integer exp in the future."""
    try:
        header, payload, sig = token.split(".")
        header_data = json.loads(_b64_decode(header))
        if header_data.get("alg") != "HS256" or header_data.get("typ") != "JWT":
            return None
        expected = _b64(hmac.new(settings.jwt_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_b64_decode(payload))
        if not isinstance(data.get("sub"), str) or not data["sub"]:
            return None
        if not isinstance(data.get("email"), str) or not data["email"]:
            return None
        if not isinstance(data.get("role"), str) or not data["role"]:
            return None
        exp = data.get("exp")
        if not isinstance(exp, int) or exp < time.time():
            return None
        return data
    except Exception:
        return None


def redact_pii(text: str) -> str:
    """Redact emails, bearer tokens, credential assignments, and
    recognisable API-token shapes from log text. Only credential-shaped
    values are redacted — plain hashes/UUIDs in logs are left alone so
    evidence stays readable."""
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]{10,}", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)[A-Za-z0-9\-_]{8,}", r"\1[REDACTED]", text)
    # Credential assignments: password=... / passwd: ... / secret=... /
    # token=... — keep the key name, drop the value.
    text = re.sub(r"(?i)(password|passwd|pwd|secret|auth[_-]?token|access[_-]?token)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)
    # Recognisable token shapes: Groq gsk_..., OpenAI sk-..., Slack xox*,
    # AWS AKIA..., GitHub ghp_/gho_/github_pat_....
    text = re.sub(r"gsk_[A-Za-z0-9]{8,}", "[REDACTED]", text)
    text = re.sub(r"sk-[A-Za-z0-9]{8,}", "[REDACTED]", text)
    text = re.sub(r"xox[baprs]-[A-Za-z0-9-]{8,}", "[REDACTED]", text)
    text = re.sub(r"AKIA[A-Z0-9]{16}", "[REDACTED]", text)
    text = re.sub(r"(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{8,}", "[REDACTED]", text)
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
