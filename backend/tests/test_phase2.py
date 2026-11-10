"""Phase 8 tests — Prometheus / Alertmanager webhook / log-provider
failure / LangGraph integration / confidence threshold / schema migration.

All external services are mocked (httpx.MockTransport / monkeypatch) —
no real Prometheus, Elasticsearch, OpenSearch or Groq infrastructure is
needed. Mirrors test_api.py: the DB is a scratch SQLite file, never the
demo incidentpilot.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_phase2.db"
for _f in ("test_phase2.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import agents as agents_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal, add_missing_columns  # noqa: E402
from app.integrations import logs as logs_mod  # noqa: E402
from app.integrations.logs import (  # noqa: E402
    ElasticsearchProvider,
    LogProviderError,
    search_logs_real,
)
from app.integrations.prometheus import PrometheusClient, PrometheusError  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Hypothesis, Incident  # noqa: E402

client = TestClient(app)

START = "2026-11-10T14:00:00Z"
END = "2026-11-10T14:15:00Z"


# --- helpers ---------------------------------------------------------------
def _prom_client_with_transport(monkeypatch, handler):
    """PrometheusClient whose HTTP layer is a MockTransport handler."""
    transport = httpx.MockTransport(handler)

    async def fake_get(self, path, params=None):
        async with httpx.AsyncClient(transport=transport, base_url=self.base_url) as c:
            return await c.get(path, params=params, headers=self._headers())

    monkeypatch.setattr(PrometheusClient, "_get", fake_get)
    return PrometheusClient(base_url="http://prom:9090")


def _query_ok(result):
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _am_payload(fingerprint="fp-test-1", status="firing", service="checkout-api"):
    return {
        "receiver": "incidentpilot",
        "status": "firing",
        "alerts": [{
            "status": status,
            "labels": {"alertname": "HighErrorRate", "service": service, "severity": "critical"},
            "annotations": {"summary": "Checkout errors high", "description": "error rate 18%"},
            "startsAt": "2026-11-10T14:00:00Z",
            "fingerprint": fingerprint,
        }],
    }


# --- Prometheus ------------------------------------------------------------
@pytest.mark.asyncio
async def test_prometheus_health_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://prom:9090/api/v1/status/buildinfo"
        return httpx.Response(200, json={"status": "success", "data": {"version": "2.52.0"}})

    prom = _prom_client_with_transport(monkeypatch, handler)
    health = await prom.health()
    assert health == {"reachable": True, "version": "2.52.0"}


@pytest.mark.asyncio
async def test_prometheus_health_connection_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    prom = _prom_client_with_transport(monkeypatch, handler)
    assert (await prom.health())["reachable"] is False
    with pytest.raises(PrometheusError, match="connection failed"):
        await prom.query('up{service="checkout-api"}')


@pytest.mark.asyncio
async def test_prometheus_health_invalid_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        # 200 but Prometheus reports an error status — not healthy.
        return httpx.Response(200, json={"status": "error", "error": "bad query"})

    prom = _prom_client_with_transport(monkeypatch, handler)
    health = await prom.health()
    assert health["reachable"] is False
    assert "error" in health


@pytest.mark.asyncio
async def test_prometheus_health_non_json_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    prom = _prom_client_with_transport(monkeypatch, handler)
    assert (await prom.health())["reachable"] is False


@pytest.mark.asyncio
async def test_prometheus_query_timeout(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    prom = _prom_client_with_transport(monkeypatch, handler)
    with pytest.raises(PrometheusError, match="timed out"):
        await prom.query('up{service="checkout-api"}')
    assert calls["n"] == 3  # 1 attempt + 2 retries


@pytest.mark.asyncio
async def test_prometheus_query_cpu_missing_metric(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_query_ok([]))  # no series for this service

    prom = _prom_client_with_transport(monkeypatch, handler)
    out = await prom.query_cpu("no-such-service")
    assert out["available"] is False
    assert out["value"] is None
    assert out["source"] == "prometheus"
    assert "no data" in (out["error"] or "")


@pytest.mark.asyncio
async def test_prometheus_query_cpu_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_query_ok(
            [{"metric": {}, "value": [1720000000, "42.5"]}]))

    prom = _prom_client_with_transport(monkeypatch, handler)
    out = await prom.query_cpu("checkout-api")
    assert out == {"available": True, "value": 42.5, "unit": "%",
                   "query": out["query"], "source": "prometheus", "error": None}


@pytest.mark.asyncio
async def test_prometheus_query_invalid_shape_raises(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {}})

    prom = _prom_client_with_transport(monkeypatch, handler)
    with pytest.raises(PrometheusError, match="invalid response"):
        await prom.query('up{service="checkout-api"}')


# --- Alertmanager / legacy webhook -----------------------------------------
def test_alertmanager_webhook_valid_creates_incident():
    r = client.post("/api/webhooks/alert", json=_am_payload(fingerprint="fp-phase8-1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["acknowledged"] is True
    assert body["incident_id"]
    detail = client.get(f"/api/incidents/{body['incident_id']}").json()
    assert detail["service"] == "checkout-api"
    assert detail["severity"] == "P1"  # critical -> P1
    assert detail["title"] == "Checkout errors high"
    assert detail["source"] == "alertmanager"


def test_alertmanager_webhook_duplicate_fingerprint_deduplicates():
    payload = _am_payload(fingerprint="fp-phase8-dup")
    first = client.post("/api/webhooks/alert", json=payload).json()
    second = client.post("/api/webhooks/alert", json=payload).json()
    assert second["deduplicated"] is True
    assert second["incident_id"] == first["incident_id"]
    assert second["deduplicated_ids"] == [first["incident_id"]]


def test_alertmanager_webhook_invalid_payload_422():
    assert client.post("/api/webhooks/alert", json={"alerts": "not-a-list"}).status_code == 422
    assert client.post("/api/webhooks/alert", json={"alerts": []}).status_code == 422
    assert client.post("/api/webhooks/alert", json={"alerts": [{"status": 123}]}).status_code == 422
    assert client.post("/api/webhooks/alert", json="just a string").status_code == 422
    assert client.post("/api/webhooks/alert", content=b"{not json").status_code == 422


def test_alertmanager_resolved_alerts_skipped():
    r = client.post("/api/webhooks/alert",
                    json=_am_payload(fingerprint="fp-phase8-resolved", status="resolved"))
    assert r.status_code == 200, r.text
    assert r.json()["incident_ids"] == []
    assert r.json()["skipped_resolved"] == 1


def test_legacy_webhook_still_works_and_deduplicates():
    body = {"title": "DB down", "service": "payments-api", "severity": "P1",
            "source": "webhook", "external_id": "legacy-phase8-1"}
    first = client.post("/api/webhooks/alert", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["deduplicated"] is False
    second = client.post("/api/webhooks/alert", json=body)
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["incident_id"] == first.json()["incident_id"]


def test_webhook_secret_enforced_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_secret", "s3cret-token")
    payload = {"title": "x", "service": "checkout-api"}
    assert client.post("/api/webhooks/alert", json=payload).status_code == 401
    assert client.post("/api/webhooks/alert", json=payload,
                       headers={"X-Alert-Token": "wrong"}).status_code == 401
    ok = client.post("/api/webhooks/alert", json=payload,
                     headers={"X-Alert-Token": "s3cret-token"})
    assert ok.status_code == 200, ok.text


def test_integrations_status_unconfigured():
    r = client.get("/api/integrations/status")
    assert r.status_code == 200
    body = r.json()
    assert body["prometheus"]["configured"] is False
    assert body["prometheus"]["reachable"] is None
    assert body["logs"]["provider"] == "none"
    assert body["logs"]["configured"] is False
    assert body["grafana"]["configured"] is False
    # No secret material is ever exposed here.
    assert "token" not in r.text.lower() and "secret" not in r.text.lower()
    assert "key" not in r.text.lower()


def test_integrations_status_logs_provider_reported(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_URL", "http://es:9200")
    monkeypatch.setattr(settings, "elasticsearch_url", "")
    monkeypatch.setattr(settings, "opensearch_url", "")
    body = client.get("/api/integrations/status").json()
    assert body["logs"]["provider"] == "elasticsearch"
    assert body["logs"]["configured"] is True


# --- Log provider failure ---------------------------------------------------
@pytest.mark.asyncio
async def test_log_provider_failure_available_false_no_invented_entries(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = ElasticsearchProvider(
        base_url="http://es:9200", transport=httpx.MockTransport(handler))
    # Phase 8: the log client must ignore proxy env vars (trust_env=False).
    assert provider._client().trust_env is False
    monkeypatch.setattr(logs_mod, "get_log_provider", lambda: provider)
    result = await search_logs_real("checkout-api", START, END)
    assert result["available"] is False
    assert result["entries"] == []
    assert result["groups"] == []
    assert result["error"] is not None
    assert "unreachable" in result["error"]


@pytest.mark.asyncio
async def test_log_provider_invalid_response_available_false(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": "not-a-dict"})

    provider = ElasticsearchProvider(
        base_url="http://es:9200", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(logs_mod, "get_log_provider", lambda: provider)
    with pytest.raises(LogProviderError, match="invalid search response"):
        await provider.search("checkout-api", START, END)
    result = await search_logs_real("checkout-api", START, END)
    assert result["available"] is False
    assert result["entries"] == []


# --- LangGraph integration: simulate + abstain + threshold ------------------
def test_simulate_checkout_oom_investigates_with_hypothesis():
    r = client.post("/api/simulate", json={"scenario": "checkout-oom"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "awaiting_approval"
    assert body["investigation"]["demo_mode"] is True
    assert "integrations" in body["investigation"]
    assert body["investigation"]["integrations"]["logs"]["provider"] == "none"
    detail = client.get(f"/api/incidents/{body['incident_id']}").json()
    assert detail["hypotheses"], "hypothesis must be present"
    assert detail["hypotheses"][0]["statement"].startswith("Memory leak")
    assert detail["hypotheses"][0]["confidence"] >= 0.65
    assert detail["plan"]["status"] == "pending"
    assert detail["monitoring"]["logs_provider"] == "none"
    assert detail["monitoring"]["prometheus_configured"] is False


def test_simulate_unknown_signal_abstains():
    r = client.post("/api/simulate", json={"scenario": "unknown-signal"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "abstained"
    assert r.json()["investigation"]["status"] == "abstained"
    assert r.json()["investigation"]["hypotheses"] == []
    detail = client.get(f"/api/incidents/{r.json()['incident_id']}").json()
    assert detail["status"] == "abstained"
    assert detail["plan"] is None  # no remediation plan when abstaining
    assert detail["hypotheses"][0]["status"] == "abstained"
    assert detail["hypotheses"][0]["confidence"] == 0.0


def test_confidence_threshold_is_065_and_enforced():
    assert settings.confidence_threshold == 0.65
    assert agents_mod.ABSTAIN_CONFIDENCE == 0.65
    # The unknown-signal case (confidence 0.0 < 0.65) must abstain, never
    # propose a plan — covered end-to-end above; here the rule itself:
    assert 0.0 < agents_mod.ABSTAIN_CONFIDENCE


@pytest.mark.asyncio
async def test_live_mode_unknown_alert_abstains_when_groq_down(monkeypatch):
    """Phase 8 guard: live mode + Groq down + no known scenario must NOT
    fall back to the checkout-oom fixture (misattribution)."""
    monkeypatch.setattr(settings, "groq_api_key", "test-key-not-real")
    assert settings.demo_mode is False

    async def _down(self, *args, **kwargs):
        raise RuntimeError("groq down")

    monkeypatch.setattr(agents_mod.GroqClient, "chat_json", _down)
    db = SessionLocal()
    try:
        inc = Incident(title="Weird spike", service="checkout-api", severity="P2",
                       source="alertmanager", external_id="phase8-guard-1", scenario="")
        db.add(inc)
        db.commit()
        db.refresh(inc)
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "abstained", out
        hyps = db.query(Hypothesis).filter_by(incident_id=inc.id).all()
        assert not any("Memory leak" in h.statement for h in hyps)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_live_mode_known_scenario_still_uses_deterministic_fallback(monkeypatch):
    """The guard is narrow: an explicitly simulated known scenario keeps
    the deterministic path when Groq is down."""
    monkeypatch.setattr(settings, "groq_api_key", "test-key-not-real")

    async def _down(self, *args, **kwargs):
        raise RuntimeError("groq down")

    monkeypatch.setattr(agents_mod.GroqClient, "chat_json", _down)
    db = SessionLocal()
    try:
        inc = Incident(title="Checkout OOM", service="checkout-api", severity="P1",
                       source="simulator", external_id="phase8-guard-2",
                       scenario="checkout-oom")
        db.add(inc)
        db.commit()
        db.refresh(inc)
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "awaiting_approval", out
    finally:
        db.close()


# --- Schema migration -------------------------------------------------------
def test_migration_adds_missing_hypothesis_columns(tmp_path):
    """An existing DB file created before Phase 6 (hypotheses table without
    the 3 JSON columns) gains them via add_missing_columns, idempotently,
    and then accepts a Hypothesis insert."""
    import sqlite3

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE hypotheses (
        id VARCHAR PRIMARY KEY, incident_id VARCHAR NOT NULL, rank INTEGER,
        statement TEXT NOT NULL, confidence FLOAT, status VARCHAR,
        evidence_ids JSON, created_at DATETIME, updated_at DATETIME)""")
    conn.commit()
    conn.close()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    add_missing_columns(engine)
    add_missing_columns(engine)  # idempotent — second run is a no-op
    cols = [r[1] for r in sqlite3.connect(str(db_path)).execute("PRAGMA table_info(hypotheses)")]
    assert "related_previous_incidents" in cols
    assert "alternatives" in cols
    assert "next_steps" in cols
    session = sessionmaker(bind=engine)()
    session.add(Hypothesis(incident_id="x", statement="t", confidence=0.9,
                           related_previous_incidents=[{"title": "t", "kind": "postmortem"}],
                           alternatives=["a"], next_steps=["s"]))
    session.commit()
    row = session.query(Hypothesis).first()
    assert row.alternatives == ["a"] and row.next_steps == ["s"]
    session.close()

# --- Groq failure instrumentation (live-mode diagnosis) ---
import asyncio
from app import agents as agents_mod

def test_groq_failure_is_recorded_not_silent(monkeypatch):
    """A failing Groq call in live mode must surface its error, not silently fall back."""
    async def _fake_groq(*a, **k):
        return {"_error": "401 Client Error: Unauthorized for url: https://api.groq.com/openai/v1/chat/completions"}
    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _fake_groq)
    from app.database import SessionLocal
    from app.models import Incident, IncidentEvent
    from app.synthetic import get_scenario
    db = SessionLocal()
    sc = get_scenario("checkout-oom")
    inc = Incident(title=sc["title"], service=sc["service"], severity=sc["severity"], source="test", external_id="groq-fail-1", scenario="checkout-oom")
    db.add(inc); db.commit(); db.refresh(inc)
    # force live mode for this test: demo_mode is a property derived from the key
    monkeypatch.setattr(agents_mod.settings, "groq_api_key", "gsk_test_key_for_test_only")
    assert agents_mod.settings.demo_mode is False
    out = asyncio.run(agents_mod.run_investigation(db, inc))
    assert out.get("groq_error") and "401" in out["groq_error"]
    evs = db.query(IncidentEvent).filter_by(incident_id=inc.id, type="groq_failed").all()
    assert len(evs) == 1 and "401" in str(evs[0].payload.get("error", ""))
    assert "gsk_" not in out["groq_error"]

def test_default_groq_model_is_current():
    """llama-3.3-70b-versatile was retired by Groq for free/dev tier (2026-08-16, HTTP 404);
    the default must be Groq's own replacement, openai/gpt-oss-120b."""
    from app.config import settings
    assert settings.groq_model == "openai/gpt-oss-120b"
