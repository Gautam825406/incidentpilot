"""Demo/simulated metrics fallback tests.

When the configured external Prometheus answers but has no series for a
simulated service (e.g. a Grafana Cloud stack nothing ever ingested
into), the Metrics agent falls back per-metric to the telemetry the
simulated services emitted for that run — labelled "demo-simulated",
never presented as production data. Pinned here:

- empty Prometheus + emitted telemetry -> demo-simulated values
  (error_rate / latency / cpu / availability); memory stays
  unavailable because the simulator does not measure it.
- a Prometheus connection/server error is NOT masked by the fallback.
- no fallback for services that are not known simulated services.
- real Prometheus data is always preferred when present.
- a full investigation records metric evidence with source_ref
  "demo-simulated" and a demo note in the content, so the Live
  Incident Dashboard shows values (with a "demo data" badge) instead
  of dashes.

All Prometheus transports are mocked (httpx.MockTransport) — no real
Prometheus or Groq call is made.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_demo_metrics_fallback.db"
for _f in ("test_demo_metrics_fallback.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import agents as agents_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.integrations.prometheus import PrometheusClient  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Evidence, Incident  # noqa: E402
from app.synthetic import get_scenario  # noqa: E402
from app.telemetry import emit_scenario_telemetry, store as telemetry_store  # noqa: E402

_ = app  # imported for its side effects (DB tables); the API is not used here.


@pytest.fixture(autouse=True)
def _reset_telemetry_store():
    telemetry_store.reset()
    yield
    telemetry_store.reset()


def _live_mode(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_key_for_test_only")
    monkeypatch.setattr(settings, "prometheus_url", "")
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    assert settings.demo_mode is False


def _mock_external_prometheus(monkeypatch, handler):
    client = PrometheusClient(base_url="http://prom.test", auth_token="tok",
                              transport=httpx.MockTransport(handler))
    monkeypatch.setattr(agents_mod, "get_prometheus_client_for_org",
                        lambda db, org_id, transport=None: client)
    return client


def _empty_prometheus(monkeypatch):
    """External Prometheus that answers fine but has no series at all."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/query":
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        return httpx.Response(404, json={"status": "error", "error": "not found"})
    return _mock_external_prometheus(monkeypatch, handler)


@pytest.mark.asyncio
async def test_fallback_returns_demo_simulated_when_prometheus_has_no_series(monkeypatch):
    _live_mode(monkeypatch)
    _empty_prometheus(monkeypatch)
    # payments-pool: 12.4% errors over 200 requests -> 25 errors -> 12.5%.
    emit_scenario_telemetry(get_scenario("payments-pool"), "run-demo-1")
    db = SessionLocal()
    try:
        out = await agents_mod._fetch_real_metrics("payments-api", run_id="run-demo-1",
                                                   db=db, org_id="org-test")
    finally:
        db.close()
    assert out["error_rate"] == {"available": True, "value": 12.5, "unit": "%",
                                 "query": "demo-simulated: error_rate from simulated-service telemetry",
                                 "source": "demo-simulated", "error": None}
    assert out["latency"]["available"] is True and out["latency"]["value"] == 3100.0
    assert out["latency"]["source"] == "demo-simulated"
    assert out["cpu"]["value"] == 68.0 and out["cpu"]["source"] == "demo-simulated"
    assert out["availability"]["value"] == 100.0 and out["availability"]["source"] == "demo-simulated"
    # Memory is not measured by the simulator, so it stays unavailable.
    assert out["memory"]["available"] is False


@pytest.mark.asyncio
async def test_no_fallback_when_prometheus_errors(monkeypatch):
    """A Prometheus failure is reported, never papered over with demo data."""
    _live_mode(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"status": "error", "error": "boom"})

    _mock_external_prometheus(monkeypatch, handler)
    emit_scenario_telemetry(get_scenario("payments-pool"), "run-demo-2")
    db = SessionLocal()
    try:
        out = await agents_mod._fetch_real_metrics("payments-api", run_id="run-demo-2",
                                                   db=db, org_id="org-test")
    finally:
        db.close()
    assert all(not (m or {}).get("available") for m in out.values())
    assert all((m or {}).get("source") == "prometheus" for m in out.values())
    assert "demo-simulated" not in str(out)


@pytest.mark.asyncio
async def test_no_fallback_for_unknown_service(monkeypatch):
    """Demo values are only for known simulated services — never for a
    service the simulator does not own."""
    _live_mode(monkeypatch)
    _empty_prometheus(monkeypatch)
    emit_scenario_telemetry(get_scenario("payments-pool"), "run-demo-3")
    db = SessionLocal()
    try:
        out = await agents_mod._fetch_real_metrics("real-prod-api", run_id="run-demo-3",
                                                   db=db, org_id="org-test")
    finally:
        db.close()
    assert all(not (m or {}).get("available") for m in out.values())
    assert "demo-simulated" not in str(out)


@pytest.mark.asyncio
async def test_prometheus_data_is_used_when_present(monkeypatch):
    """Real Prometheus data always wins over the demo fallback."""
    _live_mode(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success",
                                         "data": {"result": [{"metric": {}, "value": [0, "3.5"]}]}})

    _mock_external_prometheus(monkeypatch, handler)
    emit_scenario_telemetry(get_scenario("payments-pool"), "run-demo-4")
    db = SessionLocal()
    try:
        out = await agents_mod._fetch_real_metrics("payments-api", run_id="run-demo-4",
                                                   db=db, org_id="org-test")
    finally:
        db.close()
    assert all((m or {}).get("available") for m in out.values())
    assert all((m or {}).get("source") == "prometheus" for m in out.values())
    assert out["error_rate"]["value"] == 3.5


@pytest.mark.asyncio
async def test_investigation_records_demo_simulated_metric_evidence(monkeypatch):
    """End to end: an investigation against an empty Prometheus records
    metric evidence labelled demo-simulated — the evidence the Live
    Incident Dashboard renders."""
    _live_mode(monkeypatch)
    _empty_prometheus(monkeypatch)

    async def _llm_pool(*a, **k):
        return {"root_cause": "Database connection pool exhaustion in payments-api from a slow reporting query",
                "confidence": 0.91, "tool": "restart_service",
                "reasoning": "Connection pool exhausted in logs; error rate above baseline.",
                "_tokens": 150, "_cost": 0.0, "_latency": 60,
                "_model": "openai/gpt-oss-120b"}

    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _llm_pool)
    db = SessionLocal()
    try:
        inc = Incident(title="Payments API timeouts — connection pool exhausted",
                       service="payments-api", severity="P1", source="test",
                       external_id="phase-demo-1", scenario="payments-pool")
        db.add(inc)
        db.commit()
        db.refresh(inc)
        emit_scenario_telemetry(get_scenario("payments-pool"), inc.external_id)
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "awaiting_approval", out
        evs = db.query(Evidence).filter_by(incident_id=inc.id).all()
        demo = [e for e in evs if e.kind == "metric" and e.source_ref == "demo-simulated"]
        assert demo, [(e.kind, e.source_ref, e.content) for e in evs]
        assert any("12.5%" in e.content and "demo/simulated" in e.content for e in demo)
        # No demo-labelled value is ever passed off as Prometheus data.
        assert not any(e.kind == "metric" and e.source_ref == "prometheus"
                       and "demo/simulated" in e.content for e in evs)
        # error_rate / latency / cpu are no longer "unavailable".
        assert not any(e.kind == "integration_unavailable" and "error_rate" in e.content
                       for e in evs)
        assert not any(e.kind == "integration_unavailable" and "latency" in e.content
                       for e in evs)
    finally:
        db.close()
