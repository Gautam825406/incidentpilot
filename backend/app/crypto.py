"""Per-org credential encryption (Phase 7, SaaS multi-tenancy).

Symmetric encryption for per-org integration secrets (tokens, API keys,
webhook URLs, passwords) stored in ``OrganizationIntegration.secret_json``.
Uses Fernet (AES-128-CBC + HMAC-SHA256, from the ``cryptography`` library).

Key source: env ``CREDENTIALS_ENCRYPTION_KEY`` — a base64 urlsafe 32-byte
Fernet key (generate one with ``Fernet.generate_key()``).

Production rule (enforced by :func:`validate_credentials_key`, called at
app startup): a production deployment — live LLM mode or a Postgres
DATABASE_URL — MUST set a persistent key. Without one the app refuses to
start, because an ephemeral key silently loses every saved integration
secret on the next restart/redeploy (observed on Render, 2026-11-11:
the Demo workspace showed all integrations "not configured" after a
redeploy even though Prometheus had been configured and tested). In
local dev / tests (demo mode + SQLite) an unset key still falls back to
an ephemeral key with a loud WARNING, so local development keeps
working — but credentials encrypted with it do NOT survive a restart.

Plaintext secrets are NEVER logged. Only ``mask_secret`` output (last 4
chars) may appear in logs or API responses.
"""
import logging

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None
_ephemeral_warned = False


def get_credentials_key() -> str:
    """Return the configured Fernet key string ("" when unset).

    Reads ``settings.credentials_encryption_key`` (env
    CREDENTIALS_ENCRYPTION_KEY). Does NOT generate or return any key
    material beyond the configured value itself.
    """
    return (settings.credentials_encryption_key or "").strip()


def _get_fernet() -> Fernet:
    """Return the process-wide Fernet instance.

    Uses the configured key when it is a valid Fernet key; otherwise
    falls back to a freshly generated ephemeral key and logs the loud
    WARNING (once per process) required by the Phase 7 brief.
    """
    global _fernet, _ephemeral_warned
    if _fernet is not None:
        return _fernet
    key = get_credentials_key()
    if key:
        try:
            _fernet = Fernet(key.encode())
            return _fernet
        except Exception:
            logger.warning(
                "CREDENTIALS_ENCRYPTION_KEY is set but is not a valid Fernet key "
                "(expected a base64 urlsafe 32-byte key) — falling back to an "
                "ephemeral demo-only key; set a valid key in production")
    _fernet = Fernet(Fernet.generate_key())
    if not _ephemeral_warned:
        _ephemeral_warned = True
        logger.warning(
            "CREDENTIALS_ENCRYPTION_KEY unset — per-org credentials encrypted "
            "with an ephemeral demo-only key; set it in production")
    return _fernet


def reset_fernet_cache() -> None:
    """Drop the cached Fernet instance (tests only, so a key change is picked up)."""
    global _fernet, _ephemeral_warned
    _fernet = None
    _ephemeral_warned = False


def _is_production_deployment() -> bool:
    """True when this process looks like a shared/production deployment.

    Uses the signals the codebase already has: live LLM mode (a
    GROQ_API_KEY is set, so ``demo_mode`` is False) or a Postgres
    DATABASE_URL (a shared database, never a throwaway local run).
    Either one alone is enough — a live-mode app on SQLite still loses
    saved credentials on every restart, and a demo-mode app on Postgres
    still shares state across restarts.
    """
    return (not settings.demo_mode) or settings.is_postgres


def validate_credentials_key() -> None:
    """Fail loudly at startup when a production deployment has no usable
    persistent encryption key for per-org credentials.

    Called once at app startup (main.py, next to
    ``validate_production_settings``). Without a persistent
    CREDENTIALS_ENCRYPTION_KEY every restart/redeploy generates a fresh
    ephemeral key and all previously saved integration secrets become
    undecryptable — the workspace then silently shows every integration
    as "not configured" (observed on Render, 2026-11-11).

    - Key set and valid: passes, in any mode.
    - Key set but invalid: production raises; dev keeps the old
      ephemeral fallback (``_get_fernet`` logs the warning on first use).
    - Key unset: production raises; dev keeps the ephemeral fallback.

    The error message tells the operator exactly what to set and how to
    generate a value. It never prints a key.
    """
    key = get_credentials_key()
    if key:
        try:
            Fernet(key.encode())
            return
        except Exception:
            if _is_production_deployment():
                raise RuntimeError(
                    "CREDENTIALS_ENCRYPTION_KEY is set but is not a valid Fernet key "
                    "(expected a base64 urlsafe 32-byte key). Refusing to start: "
                    "set it to a fresh key — generate one with: python -c "
                    "\"from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())\" — set it as an "
                    "environment variable, and restart. Secrets encrypted with "
                    "the previous key cannot be recovered and must be re-entered."
                )
            return
    if _is_production_deployment():
        raise RuntimeError(
            "CREDENTIALS_ENCRYPTION_KEY is not set but this looks like a production "
            "deployment (live LLM mode or a Postgres DATABASE_URL). Refusing to "
            "start: per-org integration credentials encrypted with an ephemeral "
            "key are lost on every restart/redeploy. Set CREDENTIALS_ENCRYPTION_KEY "
            "to a persistent Fernet key — generate one with: python -c "
            "\"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\" — set it as an environment "
            "variable (Render dashboard for the backend service), and restart. "
            "Credentials saved under a previous ephemeral key cannot be recovered "
            "and must be re-entered."
        )
    # Local dev / tests: _get_fernet() logs the loud warning on first use.


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext secret; returns the Fernet token as str.

    Raises ValueError on empty input (an empty secret is a caller bug,
    not something to encrypt).
    """
    if plaintext is None or plaintext == "":
        raise ValueError("cannot encrypt an empty secret")
    return _get_fernet().encrypt(str(plaintext).encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a Fernet token produced by :func:`encrypt_secret`.

    Raises ValueError when the token is invalid or was encrypted with a
    different key (e.g. an ephemeral key lost on restart).
    """
    if not ciphertext:
        raise ValueError("cannot decrypt an empty value")
    try:
        return _get_fernet().decrypt(str(ciphertext).encode()).decode()
    except InvalidToken:
        raise ValueError("cannot decrypt secret: invalid token or wrong key")


def mask_secret(value: str | None) -> str:
    """Mask a secret for display/logs: last 4 chars only, e.g. ``"••••1234"``.

    Returns "" for an empty/None value and "••••" when the value is 4
    chars or shorter (nothing safe to reveal).
    """
    if not value:
        return ""
    value = str(value)
    if len(value) <= 4:
        return "••••"
    return "••••" + value[-4:]
