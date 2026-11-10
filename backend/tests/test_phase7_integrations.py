"""Phase 7 Step 2 tests — per-org integrations + agents (Worker B).

Covers: per-org Prometheus client (org URL/token used, global fallback),
per-org log provider selection (elasticsearch vs opensearch, fallback to
embedded), per-org Grafana dashboard URL, per-org Jira/Slack notify
(org credentials used, global fallback), the test-connection endpoint
(ok/fail, no secrets leaked, Slack never posted to), the per-org status
endpoint (masked only, ``source`` per entry), the org integrations CRUD
endpoints (admin-only writes, masked views), agents using the org
Prometheus/logs providers in live mode, and the demo org falling back
to the global/embedded providers exactly as before.

All transports are mocked (httpx.MockTransport / monkeypatch) — no real
Prometheus, Elasticsearch, Jira, Slack or Groq infrastructure is needed.
Secret values below are invented test strings, never real credentials.
"""
import base64
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_phase7_integrations.db"
for _f in ("test_phase7_integrations.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import agents as agents_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.integrations.grafana import dashboard_url_for_org  # noqa: E402
from app.integrations.jira import JiraClient, create_jira_issue_for_org  # noqa: E402
from app.integrations.logs import (  # noqa: E402
    EmbeddedLogProvider,
    _SearchProvider,
    get_log_provider_for_org,
    search_logs_real_for_org,
)
from app.integrations.prometheus import (  # noqa: E402
    EmbeddedPrometheusClient,
    PrometheusClient,
    get_prometheus_client_for_org,
)
from app.integrations.slack import SlackClient, post_slack_for_org  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Evidence, Incident  # noqa: E402
from app.org_integrations import upsert_org_integration  # noqa: E402
from app.synthetic import get_scenario  # noqa: E402
from app.telemetry import emit_scenario_telemetry  # noqa: E402
from app.agents import run_investigation  # noqa: E402

client = TestClient(app)

ORG_PROM_URL = "http://org-prom:9090"
ORG_PROM_TOKEN = "org-prom-token-SECRET1"
GLOBAL_PROM_URL = "http://global-prom:9090"
GLOBAL_PROM_TOKEN = "global-prom-token-SECRET2"
ORG_ES_URL = "http://org-es:9200"
ORG_ES_KEY = "org-es-api-key-SECRET3"
ORG_JIRA_URL = "https://org-jira.example"
ORG_JIRA_TOKEN = "org-jira-token-SECRET4"
ORG_SLACK_WEBHOOK = "https://hooks.slack.com/services/T123/ORG/WEBHOOK9"


def _uniq():
    return uuid.uuid4().hex[:8]


def _signup(company="Acme"):
    suffix = _uniq()
    email = f"admin-{suffix}@acme-{suffix}.example"
    r = client.post("/api/auth/signup", json={
        "company_name": f"{company} {suffix}", "admin_name": "Acme Admin",
        "email": email, "password": "strong-pass-123"})
    assert r.status_code == 200, r.text
    body = r.json()
    return {"email": email, "token": body["token"], "org": body["organization"]}


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _demo_login(email, password):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _db():
    return SessionLocal()


def _put_integration(token, name, provider=None, config=None, secrets=None):
    body = {"name": name, "provider": provider, "config": config or {},
            "secrets": secrets or {}}
    return client.put(f"/api/orgs/integrations/{name}", json=body, headers=_headers(token))


class _FakeIncident:
    def __init__(self, service="checkout-api", severity="P1", title="Checkout down"):
        self.id = "inc-fake-1"
        self.title = title
        self.service = service
        self.severity = severity
        self.status = "open"


# --- Prometheus: per-org client --------------------------------------------

def _prom_query_ok(result_value="42"):
    return {"status": "success", "data": {"resultType": "vector",
                                           "result": [{"metric": {}, "value": [1234567890, result_value]}]}}


@pytest.mark.asyncio
async def test_prometheus_org_client_used_not_global():
    org = _signup()
    db = _db()
    try:
        upsert_org_integration(db, org["org"]["id"], "prometheus",
                               config={"url": ORG_PROM_URL}, secrets={"token": ORG_PROM_TOKEN})
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            assert str(request.url).startswith(ORG_PROM_URL), f"org URL expected, got {request.url}"
            assert request.headers.get("Authorization") == f"Bearer {ORG_PROM_TOKEN}"
            assert GLOBAL_PROM_URL not in str(request.url)
            if request.url.path.endswith("/api/v1/status/buildinfo"):
                return httpx.Response(200, json={"status": "success", "data": {"version": "2.53"}})
            return httpx.Response(200, json=_prom_query_ok("42"))

        transport = httpx.MockTransport(handler)
        prom = get_prometheus_client_for_org(db, org["org"]["id"], transport=transport)
        assert isinstance(prom, PrometheusClient)
        assert prom.base_url == ORG_PROM_URL
        metrics = await prom.service_metrics("checkout-api")
        assert all(m["available"] and m["value"] == 42.0 for m in metrics.values())
        assert (await prom.health())["reachable"] is True
        assert seen, "org Prometheus was never contacted"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prometheus_global_fallback_when_org_has_none(monkeypatch):
    org = _signup()
    monkeypatch.setattr(settings, "prometheus_url", GLOBAL_PROM_URL)
    monkeypatch.setattr(settings, "prometheus_auth_token", GLOBAL_PROM_TOKEN)
    db = _db()
    try:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            assert str(request.url).startswith(GLOBAL_PROM_URL)
            assert request.headers.get("Authorization") == f"Bearer {GLOBAL_PROM_TOKEN}"
            return httpx.Response(200, json=_prom_query_ok("7"))

        prom = get_prometheus_client_for_org(db, org["org"]["id"],
                                             transport=httpx.MockTransport(handler))
        assert isinstance(prom, PrometheusClient)
        assert prom.base_url == GLOBAL_PROM_URL
        metrics = await prom.service_metrics("checkout-api")
        assert metrics["cpu"]["value"] == 7.0
        assert seen, "global Prometheus was never contacted"
        # org_id None also falls back to global.
        prom2 = get_prometheus_client_for_org(db, None,
                                              transport=httpx.MockTransport(handler))
        assert isinstance(prom2, PrometheusClient) and prom2.base_url == GLOBAL_PROM_URL
    finally:
        db.close()


def test_prometheus_org_none_means_embedded_when_no_global(monkeypatch):
    monkeypatch.setattr(settings, "prometheus_url", "")
    db = _db()
    try:
        assert isinstance(get_prometheus_client_for_org(db, None), EmbeddedPrometheusClient)
    finally:
        db.close()


# --- Logs: per-org provider selection --------------------------------------

def _es_search_response(message="java.lang.OutOfMemoryError: Java heap space"):
    return {"hits": {"hits": [{"_source": {
        "@timestamp": "2026-11-10T14:00:00Z", "service": "checkout-api",
        "level": "ERROR", "message": message, "source": "app.log",
        "exception": {"type": "java.lang.OutOfMemoryError"}}}] }}


@pytest.mark.asyncio
async def test_logs_org_elasticsearch_used():
    org = _signup()
    db = _db()
    try:
        upsert_org_integration(db, org["org"]["id"], "logs", provider="elasticsearch",
                               config={"url": ORG_ES_URL, "index": "org-logs"},
                               secrets={"api_key": ORG_ES_KEY})
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            assert str(request.url).startswith(ORG_ES_URL)
            assert request.headers.get("Authorization") == f"Bearer {ORG_ES_KEY}"
            assert request.url.path == "/org-logs/_search"
            return httpx.Response(200, json=_es_search_response())

        transport = httpx.MockTransport(handler)
        provider = get_log_provider_for_org(db, org["org"]["id"], transport=transport)
        assert provider.provider_name == "elasticsearch"
        assert provider.base_url == ORG_ES_URL
        assert provider.index == "org-logs"
        result = await search_logs_real_for_org(db, org["org"]["id"], "checkout-api",
                                                "2026-11-10T13:00:00Z", "2026-11-10T14:00:00Z",
                                                transport=transport)
        assert result["available"] is True
        assert result["provider"] == "elasticsearch"
        assert any("OutOfMemoryError" in e["message"] for e in result["entries"])
        assert seen, "org Elasticsearch was never contacted"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_logs_org_opensearch_used():
    org = _signup()
    db = _db()
    try:
        upsert_org_integration(db, org["org"]["id"], "logs", provider="opensearch",
                               config={"url": "http://org-os:9200", "index": "os-logs"},
                               secrets={"auth": "Basic b3JnOm9yZy1zZWNyZXQ="})
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            assert str(request.url).startswith("http://org-os:9200")
            assert request.headers.get("Authorization") == "Basic b3JnOm9yZy1zZWNyZXQ="
            return httpx.Response(200, json=_es_search_response("cache pool exhausted"))

        transport = httpx.MockTransport(handler)
        provider = get_log_provider_for_org(db, org["org"]["id"], transport=transport)
        assert provider.provider_name == "opensearch"
        entries = await search_logs_real_for_org(
            db, org["org"]["id"], "checkout-api",
            "2026-11-10T13:00:00Z", "2026-11-10T14:00:00Z", transport=transport)
        assert entries["available"] is True and entries["provider"] == "opensearch"
        assert seen, "org OpenSearch was never contacted"
    finally:
        db.close()


def test_logs_fallback_to_embedded_when_org_has_none(monkeypatch):
    monkeypatch.setattr(settings, "elasticsearch_url", "")
    monkeypatch.setattr(settings, "opensearch_url", "")
    org = _signup()
    db = _db()
    try:
        assert isinstance(get_log_provider_for_org(db, org["org"]["id"]), EmbeddedLogProvider)
        assert isinstance(get_log_provider_for_org(db, None), EmbeddedLogProvider)
    finally:
        db.close()


# --- Grafana: per-org dashboard URL ----------------------------------------

def test_grafana_org_dashboard_url():
    org = _signup()
    db = _db()
    try:
        upsert_org_integration(db, org["org"]["id"], "grafana",
                               config={"url": "http://org-grafana:3000",
                                       "dashboards": {"checkout-api": "d/org123/checkout"}})
        assert dashboard_url_for_org(db, org["org"]["id"], "checkout-api") == \
            "http://org-grafana:3000/d/org123/checkout"
        assert dashboard_url_for_org(db, org["org"]["id"], "unknown-service") is None
    finally:
        db.close()


def test_grafana_global_fallback(monkeypatch):
    monkeypatch.setattr(settings, "grafana_url", "http://global-grafana:3000")
    monkeypatch.setattr(settings, "grafana_dashboards", '{"checkout-api": "d/g1/checkout"}')
    org = _signup()
    db = _db()
    try:
        assert dashboard_url_for_org(db, org["org"]["id"], "checkout-api") == \
            "http://global-grafana:3000/d/g1/checkout"
        assert dashboard_url_for_org(db, None, "checkout-api") == \
            "http://global-grafana:3000/d/g1/checkout"
    finally:
        db.close()


# --- Jira / Slack: per-org notify ------------------------------------------

@pytest.mark.asyncio
async def test_jira_org_credentials_used():
    org = _signup()
    db = _db()
    try:
        upsert_org_integration(db, org["org"]["id"], "jira",
                               config={"url": ORG_JIRA_URL, "project": "ORG"},
                               secrets={"email": "ops@org.example", "api_token": ORG_JIRA_TOKEN})
        seen = []
        expected_auth = "Basic " + base64.b64encode(
            f"ops@org.example:{ORG_JIRA_TOKEN}".encode()).decode()

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            assert str(request.url).startswith(ORG_JIRA_URL)
            assert request.headers.get("Authorization") == expected_auth
            assert request.url.path == "/rest/api/3/issue"
            body = json.loads(request.content)
            assert body["fields"]["project"] == {"key": "ORG"}
            return httpx.Response(201, json={"key": "ORG-1", "id": "1001"})

        result = await create_jira_issue_for_org(db, org["org"]["id"], _FakeIncident(),
                                                 transport=httpx.MockTransport(handler))
        assert result == {"key": "ORG-1", "url": f"{ORG_JIRA_URL}/browse/ORG-1", "id": "1001"}
        assert seen, "org Jira was never contacted"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_slack_org_webhook_used():
    org = _signup()
    db = _db()
    try:
        upsert_org_integration(db, org["org"]["id"], "slack",
                               config={"channel": "#org-incidents"},
                               secrets={"webhook_url": ORG_SLACK_WEBHOOK})
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            assert str(request.url) == ORG_SLACK_WEBHOOK
            assert "Checkout down" in json.loads(request.content)["text"]
            return httpx.Response(200, text="ok")

        result = await post_slack_for_org(db, org["org"]["id"], "new_incident",
                                          _FakeIncident(),
                                          transport=httpx.MockTransport(handler))
        assert result == {"delivered": True, "channel": "#org-incidents"}
        assert seen, "org Slack webhook was never posted to"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_jira_slack_global_fallback(monkeypatch):
    org = _signup()  # no org integrations at all
    monkeypatch.setattr(settings, "jira_url", "https://global-jira.example")
    monkeypatch.setattr(settings, "jira_email", "ops@global.example")
    monkeypatch.setattr(settings, "jira_api_token", "global-jira-token")
    monkeypatch.setattr(settings, "jira_project_key", "GLB")
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.com/services/T999/GLOBAL/WEBHOOK")
    monkeypatch.setattr(settings, "slack_channel", "#global")
    db = _db()
    try:
        captured = {}

        async def fake_jira_request(self, method, path, payload=None):
            captured["jira_base"] = self.base_url
            captured["jira_auth"] = self._headers().get("Authorization")
            return {"key": "GLB-3", "id": "9"}

        async def fake_slack_post(self, text, channel=None):
            captured["slack_url"] = self.webhook_url
            return {"delivered": True, "channel": channel or self.channel}

        monkeypatch.setattr(JiraClient, "_request", fake_jira_request)
        monkeypatch.setattr(SlackClient, "post_message", fake_slack_post)
        result = await create_jira_issue_for_org(db, org["org"]["id"], _FakeIncident())
        assert result["key"] == "GLB-3"
        assert captured["jira_base"] == "https://global-jira.example"
        assert captured["jira_auth"] == "Basic " + base64.b64encode(
            b"ops@global.example:global-jira-token").decode()
        result = await post_slack_for_org(db, org["org"]["id"], "new_incident", _FakeIncident())
        assert result["delivered"] is True
        assert captured["slack_url"] == "https://hooks.slack.com/services/T999/GLOBAL/WEBHOOK"
    finally:
        db.close()


# --- Org integrations CRUD (masked views, admin-only writes) ---------------

def test_org_integrations_crud_masked_and_admin_only():
    org = _signup()
    token = org["token"]
    secret = "crud-secret-TOKEN9"
    r = _put_integration(token, "prometheus", config={"url": ORG_PROM_URL},
                         secrets={"token": secret})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "prometheus" and body["is_active"] is True
    assert body["secrets_masked"]["token"] == "••••KEN9"
    assert secret not in r.text

    r = client.get("/api/orgs/integrations", headers=_headers(token))
    assert r.status_code == 200, r.text
    listed = {i["name"]: i for i in r.json()["integrations"]}
    assert listed["prometheus"]["secrets_masked"]["token"] == "••••KEN9"
    assert secret not in r.text

    # Unknown path name -> 400; body/path mismatch -> 400.
    assert _put_integration(token, "bogus", config={}, secrets={}).status_code == 400
    r = client.put("/api/orgs/integrations/jira",
                   json={"name": "slack", "config": {}, "secrets": {}},
                   headers=_headers(token))
    assert r.status_code == 400

    # Responder (demo org) may not write.
    demo_responder = _demo_login("responder@demo.local", "responder123")
    r = _put_integration(demo_responder["token"], "slack", config={},
                         secrets={"webhook_url": "https://hooks.slack.com/services/X/Y/Z"})
    assert r.status_code == 403
    r = client.get("/api/orgs/integrations")  # unauthenticated
    assert r.status_code == 401

    r = client.delete("/api/orgs/integrations/prometheus", headers=_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    r = client.delete("/api/orgs/integrations/prometheus", headers=_headers(token))
    assert r.status_code == 404


# --- Status endpoint: per-org source + masked only --------------------------

def test_status_endpoint_per_org_source_and_masked(monkeypatch):
    monkeypatch.setattr(settings, "prometheus_url", "")
    monkeypatch.setattr(settings, "elasticsearch_url", "")
    monkeypatch.setattr(settings, "opensearch_url", "")
    monkeypatch.setattr(settings, "grafana_url", "")
    org = _signup()
    jira_secret = "status-secret-XYZ1"
    r = _put_integration(org["token"], "jira",
                         config={"url": ORG_JIRA_URL, "project": "ORG"},
                         secrets={"email": "ops@org.example", "api_token": jira_secret})
    assert r.status_code == 200, r.text

    r = client.get("/api/integrations/status", headers=_headers(org["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    entries = {e["name"]: e for e in body["org_integrations"]}
    assert set(entries) == {"prometheus", "logs", "grafana", "jira", "slack"}
    assert entries["jira"]["configured"] is True
    assert entries["jira"]["source"] == "org"
    assert entries["prometheus"]["source"] == "none"
    assert entries["prometheus"]["configured"] is False
    # No secret material anywhere in the response.
    assert jira_secret not in r.text
    assert ORG_JIRA_URL not in r.text  # only the masked tail may appear
    assert "••••" in r.text or entries["jira"]["masked_url"] is not None
    # Pre–Phase 7 global shape is preserved.
    assert body["prometheus"]["configured"] is False
    assert body["logs"]["provider"] == "embedded"

    # Unauthenticated callers still get a 200 (demo org) — the route
    # stays open for the frontend status panel and pre–Phase 7 tests.
    r = client.get("/api/integrations/status")
    assert r.status_code == 200
    assert r.json()["org_integrations"][0]["source"] in ("org", "global", "none")


def test_status_endpoint_source_global(monkeypatch):
    monkeypatch.setattr(settings, "prometheus_url", GLOBAL_PROM_URL)
    monkeypatch.setattr(settings, "elasticsearch_url", "")
    monkeypatch.setattr(settings, "opensearch_url", "")
    org = _signup()
    r = client.get("/api/integrations/status", headers=_headers(org["token"]))
    entries = {e["name"]: e for e in r.json()["org_integrations"]}
    assert entries["prometheus"]["configured"] is False
    assert entries["prometheus"]["source"] == "global"


# --- Test-connection endpoint -----------------------------------------------

def test_connection_endpoint_prometheus_ok_and_fail(monkeypatch):
    org = _signup()
    token = org["token"]
    secret = "test-endpoint-TOKEN7"
    assert _put_integration(token, "prometheus", config={"url": ORG_PROM_URL},
                            secrets={"token": secret}).status_code == 200

    async def fake_get_ok(self, path, params=None):
        return httpx.Response(200, json={"status": "success", "data": {"version": "2.53"}})

    monkeypatch.setattr(PrometheusClient, "_get", fake_get_ok)
    r = client.post("/api/integrations/prometheus/test", headers=_headers(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "prometheus", "ok": True, "message": "Prometheus reachable"}
    assert secret not in r.text

    async def fake_get_fail(self, path, params=None):
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", ORG_PROM_URL))

    monkeypatch.setattr(PrometheusClient, "_get", fake_get_fail)
    r = client.post("/api/integrations/prometheus/test", headers=_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert secret not in r.text

    # last_test_ok was recorded on the org integration.
    listed = {i["name"]: i for i in
              client.get("/api/orgs/integrations", headers=_headers(token)).json()["integrations"]}
    assert listed["prometheus"]["last_test_ok"] is False

    # Guards: unknown name, unauthenticated, viewer.
    assert client.post("/api/integrations/bogus/test", headers=_headers(token)).status_code == 400
    assert client.post("/api/integrations/prometheus/test").status_code == 401
    demo_viewer = _demo_login("viewer@demo.local", "viewer123")
    assert client.post("/api/integrations/prometheus/test",
                       headers=_headers(demo_viewer["token"])).status_code == 403


def test_connection_endpoint_logs_grafana_jira_slack(monkeypatch):
    org = _signup()
    token = org["token"]
    es_key = "conn-es-key-SECRET5"
    assert _put_integration(token, "logs", provider="elasticsearch",
                            config={"url": ORG_ES_URL, "index": "org-logs"},
                            secrets={"api_key": es_key}).status_code == 200
    assert _put_integration(token, "grafana",
                            config={"url": "http://org-grafana:3000", "dashboards": {}}).status_code == 200
    assert _put_integration(token, "jira", config={"url": ORG_JIRA_URL, "project": "ORG"},
                            secrets={"email": "ops@org.example",
                                     "api_token": "conn-jira-SECRET6"}).status_code == 200
    slack_secret = "conn-slack-webhook-SECRET8"
    assert _put_integration(token, "slack", config={"channel": "#org"},
                            secrets={"webhook_url": ORG_SLACK_WEBHOOK}).status_code == 200

    # Logs: provider health via a mocked ES transport.
    def es_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == f"Bearer {es_key}"
        return httpx.Response(200, json={})

    def fake_search_client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(es_handler),
                                 headers=self._headers(), trust_env=False)

    monkeypatch.setattr(_SearchProvider, "_client", fake_search_client)
    r = client.post("/api/integrations/logs/test", headers=_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and "elasticsearch" in r.json()["message"]
    assert es_key not in r.text

    # Grafana + Slack: fake httpx.AsyncClient; record every request so
    # we can prove Slack was only ever GET-checked, never POSTed to.
    calls = []

    class _FakeResponse:
        status_code = 405
        text = ""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            calls.append(("GET", str(url)))
            return _FakeResponse()

        async def post(self, url, **kwargs):
            calls.append(("POST", str(url)))
            return _FakeResponse()

    import app.main as main_mod  # noqa: E402
    monkeypatch.setattr(main_mod.httpx, "AsyncClient", _FakeAsyncClient)

    r = client.post("/api/integrations/grafana/test", headers=_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert ("GET", "http://org-grafana:3000") in calls

    r = client.post("/api/integrations/slack/test", headers=_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert "no message posted" in r.json()["message"]
    assert slack_secret not in r.text
    assert all(method == "GET" for method, _url in calls), f"Slack test posted: {calls}"

    # Jira: mocked _request (no real HTTP).
    async def fake_jira_request(self, method, path, payload=None):
        assert method == "GET" and path == "/rest/api/3/myself"
        return {"accountId": "abc"}

    monkeypatch.setattr(JiraClient, "_request", fake_jira_request)
    r = client.post("/api/integrations/jira/test", headers=_headers(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "jira", "ok": True, "message": "Jira reachable"}

    # Unconfigured integration on a fresh org reports ok False.
    fresh = _signup()
    r = client.post("/api/integrations/slack/test", headers=_headers(fresh["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert "not configured" in r.json()["message"]


# --- Notify route uses org credentials --------------------------------------

def test_notify_uses_org_jira_and_slack(monkeypatch):
    org = _signup()
    token = org["token"]
    assert _put_integration(token, "jira", config={"url": ORG_JIRA_URL, "project": "ORG"},
                            secrets={"email": "ops@org.example",
                                     "api_token": ORG_JIRA_TOKEN}).status_code == 200
    assert _put_integration(token, "slack", config={"channel": "#org"},
                            secrets={"webhook_url": ORG_SLACK_WEBHOOK}).status_code == 200

    r = client.post("/api/simulate", json={"scenario": "checkout-oom"}, headers=_headers(token))
    assert r.status_code == 200, r.text
    inc_id = r.json()["incident_id"]

    captured = {}

    async def fake_jira_request(self, method, path, payload=None):
        captured["jira_base"] = self.base_url
        captured["jira_auth"] = self._headers().get("Authorization")
        return {"key": "ORG-7", "id": "77"}

    async def fake_slack_post(self, text, channel=None):
        captured["slack_url"] = self.webhook_url
        return {"delivered": True, "channel": channel or self.channel}

    monkeypatch.setattr(JiraClient, "_request", fake_jira_request)
    monkeypatch.setattr(SlackClient, "post_message", fake_slack_post)

    r = client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "jira"},
                    headers=_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["issue_key"] == "ORG-7"
    assert captured["jira_base"] == ORG_JIRA_URL
    assert captured["jira_auth"] == "Basic " + base64.b64encode(
        f"ops@org.example:{ORG_JIRA_TOKEN}".encode()).decode()

    r = client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "jira"},
                    headers=_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["already_linked"] is True
    assert r.json()["issue_url"] == f"{ORG_JIRA_URL}/browse/ORG-7"

    r = client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "slack"},
                    headers=_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["delivered"] is True
    assert captured["slack_url"] == ORG_SLACK_WEBHOOK


# --- Agents use org providers in live mode -----------------------------------

class _FakeGroqUnavailable:
    available = False


@pytest.mark.asyncio
async def test_agents_use_org_prometheus_and_logs(monkeypatch):
    monkeypatch.setattr(settings, "prometheus_url", "")
    monkeypatch.setattr(settings, "elasticsearch_url", "")
    monkeypatch.setattr(settings, "opensearch_url", "")
    monkeypatch.setattr(settings, "groq_api_key", "test-key-not-real")
    monkeypatch.setattr(agents_mod, "GroqClient", _FakeGroqUnavailable)
    org = _signup()
    org_id = org["org"]["id"]
    db = _db()
    try:
        upsert_org_integration(db, org_id, "prometheus",
                               config={"url": ORG_PROM_URL}, secrets={"token": ORG_PROM_TOKEN})
        upsert_org_integration(db, org_id, "logs", provider="elasticsearch",
                               config={"url": ORG_ES_URL, "index": "org-logs"},
                               secrets={"api_key": ORG_ES_KEY})
        prom_seen, es_seen = [], []

        async def fake_prom_get(self, path, params=None):
            prom_seen.append((str(self.base_url), dict(self._headers())))
            assert self.base_url == ORG_PROM_URL
            assert self._headers().get("Authorization") == f"Bearer {ORG_PROM_TOKEN}"
            return httpx.Response(200, json=_prom_query_ok("37.5"))

        def es_handler(request: httpx.Request) -> httpx.Response:
            es_seen.append(request)
            assert str(request.url).startswith(ORG_ES_URL)
            assert request.headers.get("Authorization") == f"Bearer {ORG_ES_KEY}"
            return httpx.Response(200, json=_es_search_response())

        def fake_search_client(self):
            return httpx.AsyncClient(transport=httpx.MockTransport(es_handler),
                                     headers=self._headers(), trust_env=False)

        monkeypatch.setattr(PrometheusClient, "_get", fake_prom_get)
        monkeypatch.setattr(_SearchProvider, "_client", fake_search_client)

        inc = Incident(title="Checkout API down", service="checkout-api", severity="P1",
                       source="simulator", external_id=f"agent-org-{_uniq()}",
                       scenario="checkout-oom", org_id=org_id)
        db.add(inc)
        db.commit()
        db.refresh(inc)
        result = await run_investigation(db, inc)
        assert result["status"] == "awaiting_approval", result
        assert prom_seen, "org Prometheus was never queried by the agents"
        assert es_seen, "org Elasticsearch was never queried by the agents"
        contents = [e.content for e in db.query(Evidence).filter_by(incident_id=inc.id).all()]
        assert any("OutOfMemoryError" in c for c in contents), contents
        assert any("error_rate 37.5" in c for c in contents), contents
        assert all(ORG_PROM_TOKEN not in c and ORG_ES_KEY not in c for c in contents)
        assert result["integrations"]["logs"]["provider"] == "elasticsearch"
        assert result["integrations"]["prometheus"]["available"] is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_agents_demo_org_falls_back_to_embedded(monkeypatch):
    """Demo org with no org credentials behaves exactly as before:
    live mode reads the embedded (simulated-service) providers."""
    monkeypatch.setattr(settings, "prometheus_url", "")
    monkeypatch.setattr(settings, "elasticsearch_url", "")
    monkeypatch.setattr(settings, "opensearch_url", "")
    monkeypatch.setattr(settings, "grafana_url", "")
    monkeypatch.setattr(settings, "groq_api_key", "test-key-not-real")
    monkeypatch.setattr(agents_mod, "GroqClient", _FakeGroqUnavailable)
    db = _db()
    try:
        assert isinstance(get_prometheus_client_for_org(db, None), EmbeddedPrometheusClient)
        assert isinstance(get_log_provider_for_org(db, None), EmbeddedLogProvider)
        assert dashboard_url_for_org(db, None, "checkout-api") is None

        scenario = get_scenario("checkout-oom")
        external_id = f"agent-demo-{_uniq()}"
        emit_scenario_telemetry(scenario, external_id)
        inc = Incident(title=scenario["title"], service=scenario["service"],
                       severity=scenario["severity"], source="simulator",
                       external_id=external_id, scenario="checkout-oom", org_id=None)
        db.add(inc)
        db.commit()
        db.refresh(inc)
        result = await run_investigation(db, inc)
        assert result["status"] == "awaiting_approval", result
        assert result["integrations"]["logs"]["provider"] == "embedded"
        assert result["integrations"]["prometheus"]["provider"] == "embedded"
        assert result["integrations"]["prometheus"]["available"] is True
        contents = [e.content for e in db.query(Evidence).filter_by(incident_id=inc.id).all()]
        assert any("OutOfMemoryError" in c for c in contents), contents
    finally:
        db.close()
