"""Phase 8 tests — Grafana Cloud Prometheus HTTP Basic auth.

Grafana Cloud Prometheus requires HTTP Basic auth (username = instance ID,
password = API token); a Bearer token there returns 401. These tests cover:
- Basic auth header when a username is configured (client + org passthrough)
- Bearer fallback when no username is configured (self-hosted Prometheus)
- No auth header when no token is configured at all
- The per-org status endpoint reporting a saved Prometheus URL as
  configured (source "org")

All transports are mocked (httpx.MockTransport) — no real Prometheus or
Grafana Cloud infrastructure is needed. All credential-looking values
below are invented dummy strings, never real credentials.
"""
import base64
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_phase8_basic_auth.db"
for _f in ("test_phase8_basic_auth.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.integrations.prometheus import PrometheusClient, get_prometheus_client_for_org  # noqa: E402
from app.main import app  # noqa: E402
from app.org_integrations import upsert_org_integration  # noqa: E402

client = TestClient(app)

DUMMY_URL = "http://dummy-prom:9090"
DUMMY_USERNAME = "3757974"
DUMMY_TOKEN = "dummy-token-1"
GLOBAL_URL = "http://global-prom:9090"
GLOBAL_TOKEN = "dummy-global-token-2"


def _basic(username: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")


def _buildinfo_handler(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path.endswith("/api/v1/status/buildinfo")
        return httpx.Response(200, json={"status": "success", "data": {"version": "2.53"}})
    return handler


def _signup(company="Acme"):
    suffix = uuid.uuid4().hex[:8]
    email = f"admin-{suffix}@acme-{suffix}.example"
    r = client.post("/api/auth/signup", json={
        "company_name": f"{company} {suffix}", "admin_name": "Acme Admin",
        "email": email, "password": "strong-pass-123"})
    assert r.status_code == 200, r.text
    body = r.json()
    return {"email": email, "token": body["token"], "org": body["organization"]}


def _db():
    return SessionLocal()


# --- client-level auth ------------------------------------------------------

@pytest.mark.asyncio
async def test_basic_auth_header_when_username_set():
    """Grafana Cloud shape: username + token -> Basic base64(user:token)."""
    seen = []
    transport = httpx.MockTransport(_buildinfo_handler(seen))
    prom = PrometheusClient(base_url=DUMMY_URL, auth_token=DUMMY_TOKEN,
                            username=DUMMY_USERNAME, transport=transport)
    assert (await prom.health())["reachable"] is True
    assert seen, "Prometheus was never contacted"
    auth = seen[0].headers.get("Authorization")
    assert auth == _basic(DUMMY_USERNAME, DUMMY_TOKEN), f"expected Basic auth, got {auth!r}"
    assert not (auth or "").startswith("Bearer ")


@pytest.mark.asyncio
async def test_bearer_fallback_when_no_username():
    """Self-hosted shape: token but no username -> Bearer, as before."""
    seen = []
    transport = httpx.MockTransport(_buildinfo_handler(seen))
    prom = PrometheusClient(base_url=DUMMY_URL, auth_token=DUMMY_TOKEN,
                            transport=transport)
    assert (await prom.health())["reachable"] is True
    assert seen
    assert seen[0].headers.get("Authorization") == f"Bearer {DUMMY_TOKEN}"


@pytest.mark.asyncio
async def test_no_auth_header_when_no_token(monkeypatch):
    """Username alone (no token) must not invent an auth header."""
    monkeypatch.setattr(settings, "prometheus_auth_token", "")
    monkeypatch.setattr(settings, "prometheus_username", "")
    seen = []
    transport = httpx.MockTransport(_buildinfo_handler(seen))
    prom = PrometheusClient(base_url=DUMMY_URL, auth_token=None,
                            username=DUMMY_USERNAME, transport=transport)
    assert (await prom.health())["reachable"] is True
    assert seen
    assert "Authorization" not in seen[0].headers


@pytest.mark.asyncio
async def test_global_username_setting_enables_basic_auth(monkeypatch):
    """PROMETHEUS_USERNAME env fallback also selects Basic auth."""
    monkeypatch.setattr(settings, "prometheus_auth_token", DUMMY_TOKEN)
    monkeypatch.setattr(settings, "prometheus_username", DUMMY_USERNAME)
    seen = []
    transport = httpx.MockTransport(_buildinfo_handler(seen))
    prom = PrometheusClient(base_url=GLOBAL_URL, transport=transport)
    assert (await prom.health())["reachable"] is True
    assert seen
    assert seen[0].headers.get("Authorization") == _basic(DUMMY_USERNAME, DUMMY_TOKEN)


# --- per-org passthrough ----------------------------------------------------

@pytest.mark.asyncio
async def test_org_integration_username_passthrough():
    """Org prometheus config {url, username} + secret token -> Basic auth."""
    org = _signup()
    db = _db()
    try:
        upsert_org_integration(db, org["org"]["id"], "prometheus",
                               config={"url": DUMMY_URL, "username": DUMMY_USERNAME},
                               secrets={"token": DUMMY_TOKEN})
        seen = []
        transport = httpx.MockTransport(_buildinfo_handler(seen))
        prom = get_prometheus_client_for_org(db, org["org"]["id"], transport=transport)
        assert isinstance(prom, PrometheusClient)
        assert prom.base_url == DUMMY_URL
        assert prom._username == DUMMY_USERNAME
        assert (await prom.health())["reachable"] is True
        assert seen, "org Prometheus was never contacted"
        assert seen[0].headers.get("Authorization") == _basic(DUMMY_USERNAME, DUMMY_TOKEN)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_org_integration_without_username_stays_bearer():
    """Org prometheus without a username keeps Bearer (backwards compat)."""
    org = _signup()
    db = _db()
    try:
        upsert_org_integration(db, org["org"]["id"], "prometheus",
                               config={"url": DUMMY_URL},
                               secrets={"token": DUMMY_TOKEN})
        seen = []
        transport = httpx.MockTransport(_buildinfo_handler(seen))
        prom = get_prometheus_client_for_org(db, org["org"]["id"], transport=transport)
        assert isinstance(prom, PrometheusClient)
        assert (await prom.health())["reachable"] is True
        assert seen
        assert seen[0].headers.get("Authorization") == f"Bearer {DUMMY_TOKEN}"
    finally:
        db.close()


# --- status reflects a saved per-org Prometheus URL --------------------------

def test_status_shows_org_prometheus_configured():
    """GET /api/integrations/status: a saved per-org Prometheus URL must
    report configured=True with source "org" (it previously showed
    "Not configured" because the frontend only read the global URL)."""
    org = _signup()
    db = _db()
    try:
        upsert_org_integration(db, org["org"]["id"], "prometheus",
                               config={"url": DUMMY_URL, "username": DUMMY_USERNAME},
                               secrets={"token": DUMMY_TOKEN})
    finally:
        db.close()
    r = client.get("/api/integrations/status",
                   headers={"Authorization": f"Bearer {org['token']}"})
    assert r.status_code == 200, r.text
    entries = {e["name"]: e for e in r.json().get("org_integrations", [])}
    assert "prometheus" in entries
    assert entries["prometheus"]["configured"] is True
    assert entries["prometheus"]["source"] == "org"
