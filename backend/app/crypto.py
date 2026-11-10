"""Per-org credential encryption (Phase 7, SaaS multi-tenancy).

Symmetric encryption for per-org integration secrets (tokens, API keys,
webhook URLs, passwords) stored in ``OrganizationIntegration.secret_json``.
Uses Fernet (AES-128-CBC + HMAC-SHA256, from the ``cryptography`` library).

Key source: env ``CREDENTIALS_ENCRYPTION_KEY`` — a base64 urlsafe 32-byte
Fernet key (generate one with ``Fernet.generate_key()``). If unset, an
ephemeral key is generated at startup and a loud WARNING is logged:
credentials encrypted with it CANNOT be decrypted after a restart, so
this is demo-only. Set the key in production (Render env var).

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
