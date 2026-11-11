"""Regression tests for the two display bugs fixed 11 Nov 2026.

Bug 1 — wrong baseline: /api/simulate overwrote the shared baseline
store (SERVICE_STATE) with the spike value, so metric evidence read
"error_rate 18.5% (baseline 18.7%)" and the anomaly check failed on a
real spike. The true baseline is 0.4% (synthetic.py).

Bug 2 — sub-agent tokens 0: deterministic specialist agents recorded
tokens=0, so the UI showed 0 for agents that never called an LLM. They
now store NULL (rendered as "—"); a genuine 0 (demo-mode supervisor)
stays 0 and must not be rewritten to "—".

Live-mode tests run with a mocked Groq (no real LLM call) and a mocked
empty Prometheus (no series -> demo-simulated fallback), exactly like
test_demo_metrics_fallback.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_display_bugs.db"
for _f in ("test_display_bugs.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import agents as agents_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.integrations.prometheus import PrometheusClient  # noqa: E402
from app.main import app  # noqa: E402
from app.synthetic import SERVICE_STATE, get_scenario  # noqa: E402
from app.telemetry import store as telemetry_store  # noqa: E402
from app.tools import MetricsInput, get_metrics  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_telemetry_and_service_state():
    telemetry_store.reset()
    saved = {k: dict(v) for k, v in SERVICE_STATE.items()}
    yield
    telemetry_store.reset()
    SERVICE_STATE.clear()
    SERVICE_STATE.update(saved)


def _live_mode(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_key_for_test_only")
    monkeypatch.setattr(settings, "prometheus_url", "")
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    assert settings.demo_mode is False


def _empty_prometheus(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/query":
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        return httpx.Response(404, json={"status": "error", "error": "not found"})

    prom = PrometheusClient(base_url="http://prom.test", auth_token="<redacted>",
                            transport=httpx.MockTransport(handler))
    monkeypatch.setattr(agents_mod, "get_prometheus_client_for_org",
                        lambda db, org_id, transport=None: prom)
    return prom


async def _llm_oom(*a, **k):
    return {"root_cause": "Memory leak introduced in deploy v2.15.0 causing OutOfMemoryError in checkout-api",
            "confidence": 0.92, "tool": "rollback_deploy",
            "reasoning": "Error rate above baseline; OutOfMemoryError in logs.",
            "_tokens": 150, "_cost": 0.0, "_latency": 60,
            "_model": "openai/gpt-oss-120b"}


def _metric_evidence(detail):
    return [e for e in detail["evidence"] if e["kind"] == "metric"]


@pytest.mark.asyncio
async def test_checkout_oom_metric_evidence_uses_true_baseline(monkeypatch):
    """Live-mode simulate: metric evidence must say (baseline 0.4%),
    never (baseline 18.7%), and the spike must read 'above baseline'."""
    _live_mode(monkeypatch)
    _empty_prometheus(monkeypatch)
    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _llm_oom)

    r = client.post("/api/simulate", json={"scenario": "checkout-oom"})
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/incidents/{r.json()['incident_id']}").json()
    assert detail["status"] == "awaiting_approval", detail

    metrics = _metric_evidence(detail)
    assert metrics, detail["evidence"]
    contents = " | ".join(e["content"] for e in metrics)
    assert "(baseline 0.4%" in contents, contents
    assert "above baseline" in contents, contents
    assert "(baseline 18.7%" not in contents, contents
    assert "at or below baseline" not in contents, contents


def test_get_metrics_anomaly_true_after_simulate_overwrites_live_signal():
    """get_metrics must still call a spike an anomaly after /api/simulate
    has overwritten the live SERVICE_STATE error_rate with 18.7."""
    SERVICE_STATE.setdefault("checkout-api", {})["error_rate"] = 18.7
    SERVICE_STATE["checkout-api"]["error_rate_baseline"] = 0.4
    SERVICE_STATE["checkout-api"]["latency_baseline"] = 180
    out = get_metrics(MetricsInput(service="checkout-api"), get_scenario("checkout-oom"))
    assert out["error_rate_baseline"] == 0.4
    assert out["latency_baseline"] == 180
    assert out["anomaly"] is True


@pytest.mark.asyncio
async def test_sub_agent_runs_store_null_tokens_supervisor_keeps_real_tokens(monkeypatch):
    """Non-LLM agents store tokens NULL; the supervisor keeps its real
    Groq token count; total_tokens sums only the real ones."""
    _live_mode(monkeypatch)
    _empty_prometheus(monkeypatch)
    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _llm_oom)

    r = client.post("/api/simulate", json={"scenario": "checkout-oom"})
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/incidents/{r.json()['incident_id']}").json()

    runs = {run["agent"]: run for run in detail["agent_runs"]}
    for name in ("triage", "logs", "metrics", "knowledge"):
        assert runs[name]["tokens"] is None, (name, runs[name])
    assert runs["supervisor"]["tokens"] == 150
    assert detail["total_tokens"] == 150


def test_demo_mode_supervisor_zero_tokens_stays_zero():
    """Demo-mode supervisor genuinely used 0 tokens — that 0 must be
    preserved (not rewritten to NULL), so the UI can tell 'no LLM call'
    (NULL -> '—') apart from 'LLM call used 0 tokens' (0)."""
    r = client.post("/api/simulate", json={"scenario": "checkout-oom"})
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/incidents/{r.json()['incident_id']}").json()

    runs = {run["agent"]: run for run in detail["agent_runs"]}
    assert runs["supervisor"]["tokens"] == 0
    assert detail["total_tokens"] == 0
