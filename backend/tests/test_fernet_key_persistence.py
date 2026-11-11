"""Fernet key persistence tests — the 2026-11-11 Render incident.

The backend encrypts per-org integration credentials with a Fernet key
(app/crypto.py). When CREDENTIALS_ENCRYPTION_KEY was unset, the key was
ephemeral: every Render redeploy generated a fresh key and all saved
integration secrets became undecryptable, so the Demo workspace silently
showed every integration as "not configured" even though Prometheus had
been configured and tested. These tests pin the fix:

- a persistent key set via env var is used for encrypt/decrypt;
- a production deployment (live LLM mode or Postgres DATABASE_URL)
  REFUSES to start without a usable persistent key;
- local dev / tests (demo mode + SQLite) still fall back to an ephemeral
  key with a warning, so development keeps working;
- a key rotation (decrypt with a different key) still fails gracefully —
  the integration is treated as "not configured", never a crash.

All mocked / local — no real Groq, Postgres or external services. Keys
are generated fresh inside each test; no key material is hardcoded.
"""
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_fernet_key.db"
for _f in ("test_fernet_key.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

from app.config import settings  # noqa: E402
from app.crypto import (  # noqa: E402
    decrypt_secret,
    encrypt_secret,
    reset_fernet_cache,
    validate_credentials_key,
)
from app.org_integrations import _decrypted_view, _masked_secrets  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_fernet_and_settings(monkeypatch):
    """Isolate each test from the process-wide Fernet cache and the
    global settings object (both are shared with the rest of the suite)."""
    reset_fernet_cache()
    monkeypatch.setattr(settings, "credentials_encryption_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "database_url", "sqlite:///./test_fernet_key.db")
    yield
    reset_fernet_cache()


def _valid_key() -> str:
    return Fernet.generate_key().decode()


def _make_production_live_mode(monkeypatch):
    """Live LLM mode (GROQ_API_KEY set) — the Render deployment shape."""
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_key_for_test_only")
    assert settings.demo_mode is False


def _make_production_postgres(monkeypatch):
    """Shared Postgres database — also a production deployment shape."""
    monkeypatch.setattr(settings, "database_url", "postgresql://user:pass@localhost:5432/db")
    assert settings.is_postgres is True


# --- Persistent key is honoured -------------------------------------------

def test_valid_key_from_settings_roundtrips(monkeypatch):
    monkeypatch.setattr(settings, "credentials_encryption_key", _valid_key())
    validate_credentials_key()  # no raise, any mode
    cipher = encrypt_secret("my-prometheus-token-1234")
    assert cipher != "my-prometheus-token-1234"
    assert decrypt_secret(cipher) == "my-prometheus-token-1234"


def test_valid_key_passes_in_production(monkeypatch):
    _make_production_live_mode(monkeypatch)
    monkeypatch.setattr(settings, "credentials_encryption_key", _valid_key())
    validate_credentials_key()  # no raise


# --- Production without a usable key refuses to start ---------------------

def test_production_live_mode_without_key_fails(monkeypatch):
    _make_production_live_mode(monkeypatch)
    with pytest.raises(RuntimeError, match="CREDENTIALS_ENCRYPTION_KEY"):
        validate_credentials_key()


def test_production_postgres_without_key_fails(monkeypatch):
    _make_production_postgres(monkeypatch)
    with pytest.raises(RuntimeError, match="CREDENTIALS_ENCRYPTION_KEY"):
        validate_credentials_key()


def test_production_invalid_key_fails(monkeypatch):
    _make_production_live_mode(monkeypatch)
    monkeypatch.setattr(settings, "credentials_encryption_key", "not-a-valid-fernet-key")
    with pytest.raises(RuntimeError, match="CREDENTIALS_ENCRYPTION_KEY"):
        validate_credentials_key()


def test_production_error_tells_operator_how_to_generate_a_key(monkeypatch):
    _make_production_live_mode(monkeypatch)
    with pytest.raises(RuntimeError) as exc_info:
        validate_credentials_key()
    assert "Fernet.generate_key()" in str(exc_info.value)
    assert "Render" in str(exc_info.value)


# --- Dev keeps the ephemeral fallback (no fail-loud) -----------------------

def test_dev_without_key_warns_only():
    # Demo mode + SQLite: the default local-dev shape.
    assert settings.demo_mode is True
    assert settings.is_postgres is False
    validate_credentials_key()  # no raise
    # Ephemeral fallback still encrypts/decrypts within the process.
    assert decrypt_secret(encrypt_secret("dev-token")) == "dev-token"


def test_dev_invalid_key_warns_only(monkeypatch):
    monkeypatch.setattr(settings, "credentials_encryption_key", "not-a-valid-fernet-key")
    validate_credentials_key()  # no raise in dev


# --- Key rotation stays graceful ("not configured", never a crash) --------

def test_rotated_key_decrypt_fails_gracefully(monkeypatch):
    monkeypatch.setattr(settings, "credentials_encryption_key", _valid_key())
    cipher = encrypt_secret("tok-1234")
    # Simulate a redeploy with a different (ephemeral) key.
    monkeypatch.setattr(settings, "credentials_encryption_key", _valid_key())
    reset_fernet_cache()
    with pytest.raises(ValueError, match="cannot decrypt secret"):
        decrypt_secret(cipher)


def _integration_row(secret_json: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="row-1",
        org_id="org-1",
        name="prometheus",
        provider="",
        config_json="{}",
        secret_json=secret_json,
        is_active=True,
        last_tested_at=None,
        last_test_ok=None,
        created_at=None,
        updated_at=None,
    )


def test_rotated_key_integration_treated_as_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "credentials_encryption_key", _valid_key())
    cipher = encrypt_secret("tok-1234")
    row = _integration_row(json.dumps({"api_token": cipher}))
    # Rotate the key (the 2026-11-11 redeploy scenario).
    monkeypatch.setattr(settings, "credentials_encryption_key", _valid_key())
    reset_fernet_cache()
    # Decrypted view omits the undecryptable secret instead of raising.
    assert _decrypted_view(row)["secrets"] == {}
    # Masked view shows a generic placeholder, never plaintext/ciphertext.
    assert _masked_secrets(row) == {"api_token": "••••"}
