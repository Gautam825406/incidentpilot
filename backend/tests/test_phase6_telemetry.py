"""Phase 6 tests — embedded telemetry: emission wiring, run-scoped reads,
/metrics endpoint, and the Phase 5 guard with real evidence.

Covers the gaps the cancelled Phase 6 coordinator left open:
- ``emit_scenario_telemetry`` was never called anywhere, so the embedded
  store was never populated. These tests pin the wiring: /api/simulate,
  the background worker, and the eval runner all emit BEFORE the
  investigation runs, tagged with the incident's external_id (run_id).
- There was no GET /metrics endpoint.
- The agents never passed run_id, so embedded reads were time-window
  only and one eval case's signals could leak into the next.

Honesty rule pinned here: emission reads ONLY the scenario's ``logs``
and ``metrics`` — never ``root_cause`` / ``remediation_tool``. A
scenario whose telemetry does not distinguish its root cause is a
finding, not something to patch around (see the per-scenario notes in
the guard-lift test).

All Groq calls are mocked — no real Groq, Prometheus or log provider is
needed. Live mode is set via settings.groq_api_key (demo_mode is a
property derived from the key — it is never set directly).
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_phase6_telemetry.db"
for _f in ("test_phase6_telemetry.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import evals.run_eval as run_eval  # noqa: E402
from app import agents as agents_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.integrations.logs import EmbeddedLogProvider  # noqa: E402
from app.integrations.prometheus import EmbeddedPrometheusClient, get_prometheus_client  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Evidence, Hypothesis, Incident  # noqa: E402
from app.synthetic import SCENARIOS, get_scenario  # noqa: E402
from app.telemetry import emit_scenario_telemetry, store as telemetry_store  # noqa: E402
from app.worker import worker as investigation_worker  # noqa: E402

client = TestClient(app)

OOM_ROOT_CAUSE = SCENARIOS["checkout-oom"]["root_cause"]


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


def _demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "prometheus_url", "")
    assert settings.demo_mode is True


def _incident(db, scenario, external_id, **overrides):
    sc = {"title": "test", "service": "checkout-api", "severity": "P3"}
    sc.update(overrides)
    inc = Incident(title=sc["title"], service=sc["service"], severity=sc["severity"],
                   source="test", external_id=external_id, scenario=scenario)
    db.add(inc)
    db.commit()
    db.refresh(inc)
    return inc


def _now_window():
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=1), now + timedelta(minutes=1)


def _metric_500_count(text: str, service: str) -> int:
    m = re.search(
        r'http_requests_total\{service="' + re.escape(service) + r'",status="500"\} (\d+)',
        text)
    return int(m.group(1)) if m else 0


# --- emission + run-scoped reads -------------------------------------------
def test_emit_then_query_logs_scoped_by_run_id():
    """emit() populates the store; query_logs with a run_id returns only
    that run's lines — another run's lines (even for the same service)
    never leak across."""
    emit_scenario_telemetry(get_scenario("checkout-oom"), "run-A")
    emit_scenario_telemetry(get_scenario("payments-pool"), "run-B")
    start, end = _now_window()

    oom = telemetry_store.query_logs("checkout-api", start, end, run_id="run-A")
    assert len(oom) == 3  # 3 ERROR lines; the WARN deploy line is not ERROR
    assert any("OutOfMemoryError" in r["message"] for r in oom)
    assert all(r["run_id"] == "run-A" for r in oom)

    # Same service, other run: nothing.
    assert telemetry_store.query_logs("checkout-api", start, end, run_id="run-B") == []
    # Other service, its own run: the pool lines, not the OOM lines.
    pool = telemetry_store.query_logs("payments-api", start, end, run_id="run-B")
    assert len(pool) == 2
    assert any("connection pool exhausted" in r["message"] for r in pool)
    assert not any("OutOfMemoryError" in r["message"] for r in pool)


def test_emit_returns_counts_and_reads_only_logs_and_metrics():
    """Honesty: emit returns what it recorded, and the emitted telemetry
    carries no trace of the expected answer — even when the scenario dict
    is given a bogus root cause / tool, neither appears in the store."""
    sc = get_scenario("checkout-oom")
    sc["root_cause"] = "BOGUS-ROOT-CAUSE-MARKER"
    sc["remediation_tool"] = "bogus_tool_marker"
    out = emit_scenario_telemetry(sc, "run-honesty")
    assert out == {"log_lines": 4, "requests": 200}
    start, end = _now_window()
    rows = telemetry_store.query_logs(
        "checkout-api", start, end, levels=("ERROR", "WARN", "INFO"),
        run_id="run-honesty", limit=50)
    assert len(rows) == 4
    assert not any("BOGUS" in r["message"] for r in rows)
    assert "BOGUS" not in telemetry_store.prometheus_text()
    assert "Memory leak" not in telemetry_store.prometheus_text()


def test_service_metrics_computed_from_observations():
    """error_rate and p95 are COMPUTED from the emitted request
    observations (37 of 200 fail at 2400ms, the rest succeed at 180ms);
    memory is never measured, so it stays unavailable — never invented."""
    emit_scenario_telemetry(get_scenario("checkout-oom"), "run-A")
    m = telemetry_store.service_metrics("checkout-api", run_id="run-A")
    assert m["error_rate"] == {"available": True, "value": 18.5, "unit": "%"}
    assert m["latency"] == {"available": True, "value": 2400.0, "unit": "ms"}
    assert m["cpu"] == {"available": True, "value": 91.0, "unit": "%"}
    assert m["memory"] == {"available": False, "value": None, "unit": "bytes"}
    assert m["availability"] == {"available": True, "value": 100.0, "unit": "%"}
    # Another run has no data at all.
    assert telemetry_store.service_metrics("checkout-api", run_id="run-B")["error_rate"]["available"] is False


def test_prometheus_text_counters_and_no_invented_memory():
    """prometheus_text() is exposition format with cumulative counters;
    memory is omitted entirely (not measured), as are log lines and any
    root-cause wording."""
    emit_scenario_telemetry(get_scenario("checkout-oom"), "run-A")
    text = telemetry_store.prometheus_text()
    assert 'http_requests_total{service="checkout-api",status="500"} 37' in text
    assert 'http_requests_total{service="checkout-api",status="200"} 163' in text
    assert 'http_request_duration_seconds_bucket{service="checkout-api",le="+Inf"} 200' in text
    assert 'process_cpu_seconds_total{service="checkout-api"} 54.6' in text  # 91% * 60s
    assert 'service_cpu_percent{service="checkout-api"} 91.0' in text
    assert 'up{service="checkout-api"} 1' in text
    assert "memory" not in text.lower()
    assert "OutOfMemoryError" not in text
    assert "Memory leak" not in text
    # Empty store -> empty exposition, not an error.
    telemetry_store.reset()
    assert telemetry_store.prometheus_text() == ""


# --- embedded providers through the real interfaces -------------------------
@pytest.mark.asyncio
async def test_embedded_providers_return_evidence_through_real_interfaces(monkeypatch):
    """EmbeddedLogProvider / EmbeddedPrometheusClient answer through the
    same provider interfaces as the external systems (search / health /
    service_metrics), labelled "embedded", scoped by run_id."""
    monkeypatch.setattr(settings, "prometheus_url", "")
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    emit_scenario_telemetry(get_scenario("checkout-oom"), "run-A")
    start, end = _now_window()

    provider = EmbeddedLogProvider()
    assert provider.provider_name == "embedded"
    entries = await provider.search("checkout-api", start, end, level="error", run_id="run-A")
    assert len(entries) == 3
    assert any("OutOfMemoryError" in e.message for e in entries)
    assert entries[0].source == "embedded"
    assert await provider.search("checkout-api", start, end, level="error", run_id="run-B") == []
    assert (await provider.health())["reachable"] is True

    prom = EmbeddedPrometheusClient()
    assert prom.source == "embedded"
    assert (await prom.health())["reachable"] is True
    metrics = await prom.service_metrics("checkout-api", run_id="run-A")
    assert metrics["error_rate"] == {"available": True, "value": 18.5, "unit": "%",
                                     "query": "embedded: error_rate from telemetry store",
                                     "source": "embedded", "error": None}
    assert metrics["memory"]["available"] is False
    assert metrics["memory"]["source"] == "embedded"
    assert (await prom.service_metrics("checkout-api", run_id="run-B"))["error_rate"]["available"] is False

    # get_prometheus_client() defaults to the embedded client with no URL.
    assert isinstance(get_prometheus_client(), EmbeddedPrometheusClient)


@pytest.mark.asyncio
async def test_fetch_real_logs_and_metrics_scoped_by_run_id():
    """The agents' own fetch helpers pass run_id through, so a live
    investigation reads only its own incident's emitted telemetry."""
    emit_scenario_telemetry(get_scenario("checkout-oom"), "run-A")
    now = datetime.now(timezone.utc)

    logs = await agents_mod._fetch_real_logs("checkout-api", now, run_id="run-A")
    assert logs["provider"] == "embedded"
    assert logs["available"] is True
    assert any("OutOfMemoryError" in e["message"] for e in logs["entries"])
    assert logs["groups"] and logs["groups"][0]["exception"] == "java.lang.OutOfMemoryError"
    other = await agents_mod._fetch_real_logs("checkout-api", now, run_id="run-B")
    assert other["entries"] == []

    metrics = await agents_mod._fetch_real_metrics("checkout-api", run_id="run-A")
    assert metrics["error_rate"]["available"] is True
    assert metrics["error_rate"]["value"] == 18.5
    assert metrics["error_rate"]["source"] == "embedded"
    assert metrics["memory"]["available"] is False
    assert (await agents_mod._fetch_real_metrics("checkout-api", run_id="run-B"))["error_rate"]["available"] is False


# --- the Phase 5 guard with real embedded evidence ---------------------------
@pytest.mark.asyncio
async def test_live_guard_lifts_with_real_embedded_evidence(monkeypatch):
    """The run-94c3f97d gap, closed: with checkout-oom telemetry emitted
    for this incident's run, the mocked LLM's 0.91 memory-leak hypothesis
    is directly supported by usable log + metric evidence from THIS
    incident, so the guard lifts and the case proposes (confidence
    untouched). Per-scenario note: checkout-oom's logs name the failure
    mode (OutOfMemoryError / heap) and its metrics spike (18.5% errors,
    p95 2400ms), so its telemetry DOES distinguish its root cause."""
    _live_mode(monkeypatch)

    async def _llm_oom(*a, **k):
        return {"root_cause": OOM_ROOT_CAUSE, "confidence": 0.91,
                "tool": "rollback_deploy",
                "reasoning": "OutOfMemoryError in logs, error rate 18.5% vs 0.4% baseline.",
                "_tokens": 150, "_cost": 0.0, "_latency": 60,
                "_model": "openai/gpt-oss-120b"}

    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _llm_oom)
    db = SessionLocal()
    try:
        inc = _incident(db, "checkout-oom", "phase6-guard-lifts-1",
                        title="Checkout API error rate spiking after deploy", severity="P1")
        emit_scenario_telemetry(get_scenario("checkout-oom"), inc.external_id)
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "awaiting_approval", out
        assert out.get("abstention_guard") is None
        assert out["hypotheses"][0]["statement"] == OOM_ROOT_CAUSE
        assert out["hypotheses"][0]["confidence"] == 0.91
        assert out.get("tokens") == 150
        # The evidence really came from the embedded providers.
        evs = db.query(Evidence).filter_by(incident_id=inc.id).all()
        assert any(e.kind == "log" and e.source_ref == "embedded"
                   and "OutOfMemoryError" in e.content for e in evs)
        assert any(e.kind == "metric" and e.source_ref == "prometheus"
                   and "18.5%" in e.content for e in evs)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_live_guard_still_abstains_when_telemetry_absent(monkeypatch):
    """The guard is NOT weakened by the wiring: with no telemetry emitted
    for this run, the embedded providers return nothing, the usable
    evidence is KB docs only, and a confident (0.95) hallucinated memory
    leak still abstains with the guard reason — exactly the Phase 5
    failure mode."""
    _live_mode(monkeypatch)

    async def _hallucinate(*a, **k):
        return {"root_cause": OOM_ROOT_CAUSE, "confidence": 0.95,
                "tool": "rollback_deploy",
                "reasoning": "Error rate is high and checkout had a recent deploy.",
                "_tokens": 120, "_cost": 0.0, "_latency": 50,
                "_model": "openai/gpt-oss-120b"}

    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _hallucinate)
    db = SessionLocal()
    try:
        inc = _incident(db, "unknown-signal", "phase6-guard-absent-1",
                        title="Vague alert — insufficient evidence", severity="P3")
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "abstained", out
        assert out["abstention_guard"] == "phase5_abstention_guard", out
        hyps = db.query(Hypothesis).filter_by(incident_id=inc.id).all()
        assert len(hyps) == 1 and hyps[0].status == "abstained"
        assert hyps[0].confidence < 0.65
        assert "Memory leak" not in hyps[0].statement
    finally:
        db.close()


# --- /metrics endpoint + simulate wiring --------------------------------------
def test_metrics_endpoint_returns_exposition_text():
    """/metrics is unauthenticated (like /health), returns Prometheus
    exposition text, and reflects emitted telemetry."""
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "version=0.0.4" in r.headers["content-type"]
    assert r.text == ""  # store was reset by the fixture
    emit_scenario_telemetry(get_scenario("checkout-oom"), "run-metrics-1")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert 'http_requests_total{service="checkout-api",status="500"} 37' in r.text
    assert "memory" not in r.text.lower()


def test_simulate_populates_store_and_metrics_counters_move():
    """/api/simulate emits before investigating: the /metrics counters
    move by exactly one run's observations (37 errors / 163 ok)."""
    before = client.get("/metrics").text
    r = client.post("/api/simulate", json={"scenario": "checkout-oom"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "awaiting_approval"  # demo mode
    after = client.get("/metrics").text
    assert _metric_500_count(after, "checkout-api") - _metric_500_count(before, "checkout-api") == 37
    assert 'http_requests_total{service="checkout-api",status="200"}' in after


def test_integrations_status_shows_embedded_providers():
    body = client.get("/api/integrations/status").json()
    assert body["prometheus"]["provider"] == "embedded"
    assert body["logs"]["provider"] == "embedded"
    assert body["logs"]["configured"] is True


# --- background worker wiring --------------------------------------------------
@pytest.mark.asyncio
async def test_worker_emits_telemetry_for_scenario_incident(monkeypatch):
    """A scenario incident investigated through the background worker gets
    its telemetry emitted before run_investigation (demo mode here, so
    the investigation itself is the deterministic path)."""
    _demo_mode(monkeypatch)
    db = SessionLocal()
    try:
        inc = _incident(db, "payments-pool", "phase6-worker-1",
                        title="Payments API timeouts — connection pool exhausted",
                        service="payments-api", severity="P1")
        await investigation_worker._run_one(inc.id)
        start, end = _now_window()
        rows = telemetry_store.query_logs("payments-api", start, end, run_id=inc.external_id)
        assert len(rows) == 2
        assert any("connection pool exhausted" in r["message"] for r in rows)
        m = telemetry_store.service_metrics("payments-api", run_id=inc.external_id)
        assert m["error_rate"]["value"] == pytest.approx(12.5)  # round(12.4% of 200)=25/200
    finally:
        db.close()


@pytest.mark.asyncio
async def test_worker_emits_nothing_for_adhoc_alert(monkeypatch):
    """Ad-hoc / Alertmanager alerts have no scenario, so the worker emits
    nothing for them — they must abstain rather than read a fixture's
    telemetry."""
    _demo_mode(monkeypatch)
    db = SessionLocal()
    try:
        inc = _incident(db, "", "phase6-worker-adhoc-1",
                        title="Weird spike", service="checkout-api", severity="P2")
        await investigation_worker._run_one(inc.id)
        assert telemetry_store.prometheus_text() == ""
    finally:
        db.close()


# --- eval runner wiring ---------------------------------------------------------
@pytest.mark.asyncio
async def test_eval_runner_emits_telemetry_per_case(monkeypatch):
    """run_all() emits each case's telemetry before investigating it, so
    every eval incident has its own run-scoped batch in the store (the
    two checkout-api cases are separated by run_id, not by service)."""
    _demo_mode(monkeypatch)
    results_path = os.path.join(os.path.dirname(run_eval.__file__), "results.json")
    with open(results_path, "rb") as f:
        original_results = f.read()
    db = SessionLocal()
    try:
        before = {i.external_id for i in db.query(Incident).filter_by(source="eval").all()}
        metrics = await run_eval.run_all(db)
        assert metrics["n"] == 6
        fresh = [i for i in db.query(Incident).filter_by(source="eval").all()
                 if i.external_id not in before]
        assert len(fresh) == 6
        for inc in fresh:
            m = telemetry_store.service_metrics(inc.service, run_id=inc.external_id)
            assert m["_requests_total"] == 200, inc.scenario
            if inc.scenario != "unknown-signal":
                start, end = _now_window()
                rows = telemetry_store.query_logs(inc.service, start, end,
                                                  run_id=inc.external_id)
                assert rows, f"no error logs emitted for {inc.scenario}"
    finally:
        db.close()
        with open(results_path, "wb") as f:
            f.write(original_results)
