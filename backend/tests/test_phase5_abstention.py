"""Phase 5 Step 1 tests — abstention fix.

Live eval run dbd04041 (openai/gpt-oss-120b, n=6) gave abstention 0/1:
the unknown-signal case hallucinated a memory leak at 0.95 confidence
instead of abstaining. These tests pin the fix: a strengthened supervisor
prompt plus a deterministic server-side guard in run_investigation that
does NOT trust the LLM's own confidence.

All Groq calls are mocked — no real Groq, Prometheus or log provider is
needed. Live mode is set via settings.groq_api_key (demo_mode is a
property derived from the key — it is never set directly).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_phase5_abstention.db"
for _f in ("test_phase5_abstention.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import pytest  # noqa: E402

from app import agents as agents_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Hypothesis, Incident  # noqa: E402
from app.synthetic import SCENARIOS  # noqa: E402

HALLUCINATED = {
    "root_cause": "Memory leak introduced in deploy v2.15.0 causing OutOfMemoryError in checkout-api",
    "confidence": 0.95,
    "tool": "rollback_deploy",
    "reasoning": "Error rate is high and checkout had a recent deploy.",
    "_tokens": 120, "_cost": 0.0, "_latency": 50, "_model": "openai/gpt-oss-120b",
}

OOM_ROOT_CAUSE = SCENARIOS["checkout-oom"]["root_cause"]


def _live_mode(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_key_for_test_only")
    assert settings.demo_mode is False


def _demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "")
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


def _stored_hypotheses(db, incident_id):
    return db.query(Hypothesis).filter_by(incident_id=incident_id).all()


# --- (b) the guard: confident hallucination must abstain -------------------
@pytest.mark.asyncio
async def test_live_unknown_signal_abstains_despite_confident_hallucination(monkeypatch):
    """The dbd04041 failure, reproduced with a mocked LLM: unknown-signal,
    no log/metric provider configured (usable evidence is KB docs only),
    mocked Groq returns a 0.95-confidence memory leak. Must abstain, with
    confidence capped below 0.65 — the guard must not trust the LLM."""
    _live_mode(monkeypatch)

    async def _hallucinate(*a, **k):
        return dict(HALLUCINATED)

    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _hallucinate)
    db = SessionLocal()
    try:
        inc = _incident(db, "unknown-signal", "phase5-halluc-1",
                        title="Vague alert — insufficient evidence", severity="P3")
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "abstained", out
        assert out["abstention_guard"] == "phase5_abstention_guard", out
        assert out.get("groq_error") is None  # LLM call succeeded; nothing failed
        hyps = _stored_hypotheses(db, inc.id)
        assert len(hyps) == 1
        assert hyps[0].status == "abstained"
        assert hyps[0].confidence < 0.65, hyps[0].confidence
        assert "Memory leak" not in hyps[0].statement
        assert hyps[0].statement.startswith("Insufficient evidence")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_live_adhoc_alert_abstains_despite_confident_hallucination(monkeypatch):
    """Same guard, no scenario at all (ad-hoc alert, scenario ''): the
    scenario has no known root cause, so a confident LLM guess abstains."""
    _live_mode(monkeypatch)

    async def _hallucinate(*a, **k):
        return dict(HALLUCINATED)

    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _hallucinate)
    db = SessionLocal()
    try:
        inc = _incident(db, "", "phase5-halluc-2",
                        title="Weird spike", severity="P2")
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "abstained", out
        assert out["abstention_guard"] == "phase5_abstention_guard", out
    finally:
        db.close()


@pytest.mark.asyncio
async def test_live_well_evidenced_case_still_proposes(monkeypatch):
    """The guard must not abstain everything: a known scenario whose LLM
    root cause is directly supported by usable log + metric evidence from
    this incident still proposes (confidence untouched at 0.91)."""
    _live_mode(monkeypatch)

    async def _llm_oom(*a, **k):
        return {"root_cause": OOM_ROOT_CAUSE, "confidence": 0.91,
                "tool": "rollback_deploy",
                "reasoning": "OutOfMemoryError in logs, error rate 18.7% vs 0.4% baseline.",
                "_tokens": 150, "_cost": 0.0, "_latency": 60,
                "_model": "openai/gpt-oss-120b"}

    async def _fake_logs(service, created_at, run_id=None):
        return {"provider": "elasticsearch", "available": True,
                "entries": [
                    {"message": "java.lang.OutOfMemoryError: Java heap space at pricing.PricingEngine.calculate",
                     "timestamp": "2026-11-10T14:01:12Z", "level": "ERROR",
                     "exception_type": "java.lang.OutOfMemoryError"},
                    {"message": "heap usage 98% — GC thrashing, 12 Full GC in 60s",
                     "timestamp": "2026-11-10T14:01:40Z", "level": "ERROR"},
                ],
                "groups": [{"exception": "java.lang.OutOfMemoryError", "count": 12,
                            "first_seen": "2026-11-10T14:01:12Z",
                            "last_seen": "2026-11-10T14:02:31Z"}],
                "error": None}

    async def _fake_metrics(service, run_id=None):
        def _m(value, unit, query=""):
            return {"available": True, "value": value, "unit": unit, "query": query}
        return {"error_rate": _m(18.7, "%"), "latency": _m(2400, "ms"),
                "cpu": _m(91.0, "%"), "memory": _m(98.0, "%"),
                "availability": _m(99.1, "%")}

    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _llm_oom)
    monkeypatch.setattr(agents_mod, "_fetch_real_logs", _fake_logs)
    monkeypatch.setattr(agents_mod, "_fetch_real_metrics", _fake_metrics)
    db = SessionLocal()
    try:
        inc = _incident(db, "checkout-oom", "phase5-evidenced-1",
                        title="Checkout API error rate spiking after deploy", severity="P1")
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "awaiting_approval", out
        assert out.get("abstention_guard") is None
        assert out["hypotheses"][0]["statement"].startswith("Memory leak")
        assert out["hypotheses"][0]["confidence"] == 0.91
        assert out.get("groq_error") is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_live_docs_only_correct_guess_still_abstains(monkeypatch):
    """Guard strictness check: even a CORRECT root cause abstains when the
    only usable evidence is KB docs — a runbook match is not observation
    of this incident. The guard is not weakened to make numbers look better."""
    _live_mode(monkeypatch)

    async def _llm_correct_but_unevidenced(*a, **k):
        return {"root_cause": OOM_ROOT_CAUSE, "confidence": 0.88,
                "tool": "rollback_deploy", "reasoning": "The runbook describes this.",
                "_tokens": 100, "_cost": 0.0, "_latency": 40,
                "_model": "openai/gpt-oss-120b"}

    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _llm_correct_but_unevidenced)
    db = SessionLocal()
    try:
        inc = _incident(db, "checkout-oom", "phase5-docs-only-1",
                        title="Checkout API error rate spiking after deploy", severity="P1")
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "abstained", out
        assert out["abstention_guard"] == "phase5_abstention_guard", out
    finally:
        db.close()


# --- (b) guard unit checks ---------------------------------------------------
def test_support_check_requires_log_or_metric_evidence():
    """Direct unit test of the deterministic check: docs alone never count,
    log evidence naming the failure mode does, unrelated evidence does not."""
    check = agents_mod._root_cause_supported_by_observational_evidence
    assert check(OOM_ROOT_CAUSE, []) is False
    assert check("", ["OutOfMemoryError heap exhaustion"]) is False
    # "memory" + "outofmemoryerror" both appear (as substrings) in the log line.
    assert check(OOM_ROOT_CAUSE,
                 ["java.lang.OutOfMemoryError: Java heap space"]) is True
    # A mismatched cause (cache storm) is not supported by OOM logs.
    assert check("Cache miss storm in search-api after an accidental cache flush",
                 ["java.lang.OutOfMemoryError: Java heap space",
                  "error_rate 18.7% (baseline 0.4%, above baseline)"]) is False


# --- (a) the strengthened prompt ---------------------------------------------
@pytest.mark.asyncio
async def test_groq_prompt_requires_abstention_and_forbids_invention(monkeypatch):
    """Capture the system prompt _groq_hypothesis sends: it must require
    abstention (confidence < 0.65, tool none) when evidence names no
    specific root cause, and forbid inventing details not in the evidence."""
    _live_mode(monkeypatch)
    captured = {}

    async def _capture(self, system, user, timeout=25.0):
        captured["system"] = system
        return {"data": {"root_cause": "", "confidence": 0.0, "tool": "none",
                         "reasoning": "insufficient evidence"},
                "tokens": 10, "cost_usd": 0.0, "latency_ms": 5,
                "model": "openai/gpt-oss-120b"}

    monkeypatch.setattr(agents_mod.GroqClient, "chat_json", _capture)
    inc = Incident(title="t", service="checkout-api", severity="P3", scenario="unknown-signal")
    result = await agents_mod._groq_hypothesis(inc, SCENARIOS["unknown-signal"], "some evidence")
    assert result is not None and "_error" not in result
    system = captured["system"].lower()
    assert "abstain" in system
    assert "0.65" in system
    assert "none" in system
    assert "never invent" in system
    assert "runbook" in system  # past-incident docs are not evidence for this one


# --- (c) groq_error recording intact ------------------------------------------
@pytest.mark.asyncio
async def test_groq_error_still_recorded_when_llm_fails(monkeypatch):
    """Phase 4 behaviour unchanged: a failing Groq call records groq_error
    and the known-scenario deterministic fallback still applies."""
    _live_mode(monkeypatch)

    async def _fake_groq(*a, **k):
        return {"_error": "401 Client Error: Unauthorized for url: https://api.groq.com/openai/v1/chat/completions"}

    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _fake_groq)
    db = SessionLocal()
    try:
        inc = _incident(db, "checkout-oom", "phase5-groq-fail-1",
                        title="Checkout API error rate spiking after deploy", severity="P1")
        out = await agents_mod.run_investigation(db, inc)
        assert out.get("groq_error") and "401" in out["groq_error"]
        assert "gsk_" not in out["groq_error"]
        assert out["status"] == "awaiting_approval", out  # known-scenario fallback
    finally:
        db.close()


# --- demo-mode deterministic path unchanged -----------------------------------
@pytest.mark.asyncio
async def test_demo_mode_checkout_oom_unchanged(monkeypatch):
    """Demo mode never touches the LLM or the guard: checkout-oom still
    proposes at 0.91 (error spike + logs + runbook = 3 signals)."""
    _demo_mode(monkeypatch)
    db = SessionLocal()
    try:
        inc = _incident(db, "checkout-oom", "phase5-demo-oom-1",
                        title="Checkout API error rate spiking after deploy", severity="P1")
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "awaiting_approval", out
        assert out["hypotheses"][0]["statement"].startswith("Memory leak")
        assert out["hypotheses"][0]["confidence"] == 0.91
    finally:
        db.close()


@pytest.mark.asyncio
async def test_demo_mode_unknown_signal_still_abstains(monkeypatch):
    _demo_mode(monkeypatch)
    db = SessionLocal()
    try:
        inc = _incident(db, "unknown-signal", "phase5-demo-unknown-1",
                        title="Vague alert — insufficient evidence", severity="P3")
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "abstained", out
        hyps = _stored_hypotheses(db, inc.id)
        assert hyps and hyps[0].status == "abstained"
        assert hyps[0].confidence == 0.0
    finally:
        db.close()


def test_threshold_still_065():
    assert settings.confidence_threshold == 0.65
    assert agents_mod.ABSTAIN_CONFIDENCE == 0.65
    assert agents_mod.PROMPT_VERSION == "v2"  # prompt changed, version bumped


@pytest.mark.asyncio
async def test_live_llm_explicit_abstention_is_not_overridden_by_fallback(monkeypatch):
    """Phase 5b: if Groq answers but abstains (no root cause), live mode must
    NOT fall back to the deterministic fixture answer. That override made the
    post-Phase-5 live eval report deterministic 0.67 fixture answers as if
    the LLM had produced them (supervisor model 'demo', tokens 0)."""
    _live_mode(monkeypatch)

    async def _abstain(*a, **k):
        return {"root_cause": "", "confidence": 0.0, "tool": "none",
                "reasoning": "Evidence does not name a specific root cause.",
                "_tokens": 210, "_cost": 0.00007, "_latency": 900,
                "_model": "openai/gpt-oss-120b"}

    monkeypatch.setattr(agents_mod, "_groq_hypothesis", _abstain)
    db = SessionLocal()
    try:
        inc = _incident(db, "payments-pool", "phase5b-abstain-1",
                        title="Payments API timeouts — connection pool exhausted", severity="P1")
        out = await agents_mod.run_investigation(db, inc)
        assert out["status"] == "abstained", out
        assert out.get("tokens") == 210, out
        hyps = _stored_hypotheses(db, inc.id)
        assert hyps and hyps[0].status == "abstained"
        assert "connection pool" not in hyps[0].statement.lower()
        # Supervisor agent run must record the LLM, not the demo fallback.
        from app.models import AgentRun
        runs = db.query(AgentRun).filter_by(incident_id=inc.id, agent="supervisor").all()
        assert runs and runs[0].model == "openai/gpt-oss-120b" and runs[0].tokens == 210
    finally:
        db.close()
