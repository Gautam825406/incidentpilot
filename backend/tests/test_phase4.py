"""Phase 4 Step 2 tests — production hardening.

Covers: per-user random password salts (old fixed-salt hashes still
verify), CORS allow-list config, JWT_SECRET fail-loud validation,
in-memory rate limiting (login + webhook, incl. 429), the background
investigation worker (webhook returns immediately, investigation
completes and is readable via GET), and the Postgres primary-DB path
(SQLite stays the local/demo fallback). All mocked / local — no real
Postgres, Groq or other infrastructure needed.
"""
import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_phase4.db"
for _f in ("test_phase4.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from app.config import DEMO_JWT_SECRET, Settings, validate_production_settings  # noqa: E402
from app.database import SessionLocal, normalize_database_url  # noqa: E402
from app.main import app, login_limiter, webhook_limiter  # noqa: E402
from app.models import User  # noqa: E402
from app.rate_limit import RateLimiter  # noqa: E402
from app.security import hash_password, verify_password  # noqa: E402

client = TestClient(app)


# --- Password salts: new random salt, legacy fixed salt still works --------
def test_new_hash_uses_random_salt_and_verifies():
    h1 = hash_password("s3cret-pw")
    h2 = hash_password("s3cret-pw")
    assert "$" in h1 and "$" in h2
    salt1, digest1 = h1.split("$", 1)
    assert len(salt1) == 32 and len(digest1) == 64  # 128-bit salt, SHA-256 hex
    assert h1 != h2, "same password must hash differently (random salt)"
    assert verify_password("s3cret-pw", h1) is True
    assert verify_password("s3cret-pw", h2) is True
    assert verify_password("wrong-pw", h1) is False


def test_legacy_fixed_salt_hash_still_verifies():
    """Pre–Phase 4 demo users were seeded with one fixed salt and a bare
    hex hash (no salt stored). Those logins must keep working."""
    legacy = hashlib.pbkdf2_hmac(
        "sha256", b"admin123", b"incidentpilot-demo-salt", 100_000).hex()
    assert "$" not in legacy
    assert verify_password("admin123", legacy) is True
    assert verify_password("not-the-password", legacy) is False
    # And the seeded demo login still works end to end.
    r = client.post("/api/auth/login",
                    json={"email": "admin@demo.local", "password": "admin123"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "admin"


def test_new_user_with_random_salt_hash_can_login():
    db = SessionLocal()
    try:
        email = "phase4-new-user@demo.local"
        if not db.query(User).filter_by(email=email).first():
            db.add(User(name="Phase4 User", email=email,
                        password_hash=hash_password("phase4-pw"), role="viewer"))
            db.commit()
    finally:
        db.close()
    r = client.post("/api/auth/login",
                    json={"email": "phase4-new-user@demo.local", "password": "phase4-pw"})
    assert r.status_code == 200, r.text
    assert client.post("/api/auth/login",
                       json={"email": "phase4-new-user@demo.local",
                             "password": "wrong"}).status_code == 401


def test_hash_password_explicit_salt_is_deterministic():
    assert hash_password("pw", salt="a" * 32) == hash_password("pw", salt="a" * 32)


# --- CORS allow-list --------------------------------------------------------
def test_cors_origin_list_default():
    s = Settings()
    assert s.cors_origin_list == [
        "http://localhost:5173",
        "http://localhost:3000",
        "https://incidentpilot-ten.vercel.app",
    ]


def test_cors_origin_list_custom_and_star(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example.com, https://b.example.com ")
    assert Settings().cors_origin_list == ["https://a.example.com", "https://b.example.com"]
    monkeypatch.setenv("CORS_ORIGINS", "*")
    assert Settings().cors_origin_list == ["*"]  # "*" only when explicitly set


# --- JWT_SECRET fail-loud ----------------------------------------------------
def test_jwt_secret_demo_default_in_live_mode_fails(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_for_test_only")
    s = Settings()
    assert s.demo_mode is False
    assert s.jwt_secret == DEMO_JWT_SECRET
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        validate_production_settings(s)


def test_jwt_secret_demo_default_in_demo_mode_warns_only(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = Settings()
    assert s.demo_mode is True
    validate_production_settings(s)  # no raise


def test_jwt_secret_custom_in_live_mode_passes(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_for_test_only")
    monkeypatch.setenv("JWT_SECRET", "a-strong-custom-secret-for-tests")
    s = Settings()
    assert s.demo_mode is False
    validate_production_settings(s)  # no raise


# --- Rate limiting ----------------------------------------------------------
def test_rate_limiter_sliding_window_unit():
    limiter = RateLimiter(limit=2, window_seconds=60.0)
    assert limiter.allow("1.2.3.4", now=1000.0) is True
    assert limiter.allow("1.2.3.4", now=1001.0) is True
    assert limiter.allow("1.2.3.4", now=1002.0) is False  # over the limit
    assert limiter.allow("5.6.7.8", now=1002.0) is True  # per-key
    assert limiter.allow("1.2.3.4", now=1061.0) is True  # window slid past
    limiter.reset()
    assert limiter.allow("1.2.3.4", now=1062.0) is True


def test_login_rate_limited_429(monkeypatch):
    monkeypatch.setattr(login_limiter, "limit", 3)
    for _ in range(3):
        r = client.post("/api/auth/login",
                        json={"email": "admin@demo.local", "password": "admin123"})
        assert r.status_code == 200, r.text
    r = client.post("/api/auth/login",
                    json={"email": "admin@demo.local", "password": "admin123"})
    assert r.status_code == 429, r.text
    assert "Too many login attempts" in r.json()["detail"]
    # The 429 message must not leak account or internal details.
    assert "admin@demo.local" not in r.json()["detail"]


def test_webhook_rate_limited_429(monkeypatch):
    monkeypatch.setattr(webhook_limiter, "limit", 2)
    for i in range(2):
        r = client.post("/api/webhooks/alert", json={
            "title": "rate limit probe", "service": "checkout-api",
            "severity": "P3", "source": "test", "external_id": f"rl-probe-{i}"})
        assert r.status_code == 200, r.text
    r = client.post("/api/webhooks/alert", json={
        "title": "rate limit probe", "service": "checkout-api",
        "severity": "P3", "source": "test", "external_id": "rl-probe-3"})
    assert r.status_code == 429, r.text
    assert "Too many alert requests" in r.json()["detail"]


def test_rate_limiting_disabled_via_settings(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(login_limiter, "limit", 1)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    for _ in range(3):
        r = client.post("/api/auth/login",
                        json={"email": "admin@demo.local", "password": "admin123"})
        assert r.status_code == 200, r.text


# --- Background worker ------------------------------------------------------
def test_webhook_returns_immediately_then_investigation_completes():
    """Webhook response contract unchanged (same shape as before), and the
    investigation — now run by the in-process worker queue — completes
    and is readable via GET /api/incidents/{id} afterwards."""
    r = client.post("/api/webhooks/alert", json={
        "title": "Checkout errors high", "service": "checkout-api",
        "severity": "P1", "source": "webhook", "external_id": "phase4-bg-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deduplicated"] is False
    assert body["incident_id"]
    assert body["status"] in ("open", "investigating")  # not yet investigated
    deadline = time.time() + 20
    detail = {}
    while time.time() < deadline:
        detail = client.get(f"/api/incidents/{body['incident_id']}").json()
        if detail["status"] in ("awaiting_approval", "abstained"):
            break
        time.sleep(0.25)
    assert detail["status"] == "awaiting_approval", detail
    assert detail["hypotheses"], "background investigation must produce hypotheses"
    assert detail["hypotheses"][0]["statement"].startswith("Memory leak")
    assert detail["plan"]["status"] == "pending"


def test_simulate_still_synchronous_with_inline_investigation():
    """Contract check: /api/simulate keeps returning the investigation
    inline (it did NOT move to the background queue)."""
    r = client.post("/api/simulate", json={"scenario": "checkout-oom"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "awaiting_approval"
    assert body["investigation"]["hypotheses"]
    assert body["investigation"]["hypotheses"][0]["statement"].startswith("Memory leak")


# --- Postgres primary path / SQLite fallback ---------------------------------
def test_postgres_url_recognised_and_sqlite_is_fallback(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/incidentpilot")
    pg = Settings()
    assert pg.is_postgres is True
    assert pg.is_sqlite is False
    # Engine creation needs the driver (psycopg2-binary, Phase 4) but no
    # server connection — the dialect check proves the URL is usable.
    engine = create_engine(pg.database_url)
    assert engine.dialect.name == "postgresql"

    monkeypatch.setenv("DATABASE_URL", "sqlite:///./incidentpilot.db")
    lite = Settings()
    assert lite.is_sqlite is True
    assert lite.is_postgres is False
    assert create_engine(lite.database_url).dialect.name == "sqlite"


def test_postgres_scheme_normalised():
    assert normalize_database_url("postgres://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert normalize_database_url("postgresql://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert normalize_database_url("sqlite:///./incidentpilot.db") == "sqlite:///./incidentpilot.db"
