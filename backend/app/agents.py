"""LangGraph supervisor + specialist agents (PRD §8).

Node functions are pure and testable. The LangGraph StateGraph (built in
build_graph()) wires them together with a MemorySaver checkpointer so an
investigation is resumable; run_investigation() executes the same nodes
in parallel (asyncio.gather) and persists every step to the DB.

Two modes:
- Live (GROQ_API_KEY set): Triage/Logs/Metrics/Knowledge call Groq
  openai/gpt-oss-120b and return structured JSON; the supervisor merges
  and enforces citation coverage.
- Demo (no key): deterministic fixture-based reasoning over the synthetic
  scenario — labelled demo_mode everywhere. It never pretends to be an LLM.

Phase 6 — real integrations: in live mode the Logs agent queries the
configured log provider (Elasticsearch/OpenSearch) and the Metrics agent
queries Prometheus; when a real integration is configured but unavailable
(or not configured at all in live mode) the agents record an
"integration_unavailable" evidence item and collect NO evidence from that
source — values are never invented. Demo mode keeps using the synthetic
fixtures, labelled demo/synthetic.
"""
import asyncio
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .config import settings
from .integrations.grafana import dashboard_url
from .integrations.logs import get_log_provider, search_logs_real
from .integrations.prometheus import get_prometheus_client
from .llm import GroqClient
from .models import AgentRun, Evidence, Hypothesis, Incident, IncidentEvent, RemediationPlan
from .security import sanitise_evidence
from .synthetic import SCENARIOS, SERVICE_STATE, get_scenario
from .tools import KnowledgeInput, MetricsInput, SearchLogsInput, get_error_clusters, get_metrics, search_knowledge, search_logs

PROMPT_VERSION = "v2"
ABSTAIN_CONFIDENCE = settings.confidence_threshold

# Phase 5 abstention guard — words too generic to count as direct support
# for a proposed root cause (service names, "deploy", "error", ...). A
# root cause is only "directly supported" when distinctive words from it
# appear in log/metric evidence from THIS incident.
_GUARD_STOPWORDS = frozenset({
    "service", "error", "errors", "failure", "failed", "issue", "problem",
    "incident", "alert", "check", "deploy", "deployed", "introduced",
    "causing", "caused", "after", "with", "from", "that", "this", "when",
    "into", "high", "rising", "spike", "slow", "unknown", "java", "lang",
    "checkout", "payments", "search", "auth", "inventory",
})


def _root_cause_supported_by_observational_evidence(root_cause: str, evidence_texts: list[str]) -> bool:
    """Deterministic support check (Phase 5).

    Returns True only when at least two distinctive words from the proposed
    root cause appear (as substrings) in usable LOG or METRIC evidence from
    this incident. Knowledge-base docs are deliberately excluded: a runbook
    or postmortem describing a similar past incident is guidance, not
    observation of this incident, so it can never directly support a root
    cause on its own. With no log/metric evidence the answer is always
    False — the guard then abstains rather than trusting the LLM.
    """
    if not root_cause or not evidence_texts:
        return False
    combined = " ".join(evidence_texts).lower()
    tokens = re.findall(r"[a-z0-9]+", root_cause.lower())
    kept = {t for t in tokens if len(t) >= 4 and t not in _GUARD_STOPWORDS}
    hits = {t for t in kept if t in combined}
    return len(hits) >= 2

# Remediation templates keyed by the tool the evidence supports.
PLAN_TEMPLATES = {
    "rollback_deploy": [
        {"tool": "rollback_deploy", "args": {}, "description": "Roll back the latest deploy to the previous known-good version", "risk": "low"},
        {"tool": "run_health_check", "args": {}, "description": "Run health check to confirm the service is responding", "risk": "low"},
    ],
    "restart_service": [
        {"tool": "restart_service", "args": {}, "description": "Restart the service to drain the faulty state", "risk": "medium"},
        {"tool": "run_health_check", "args": {}, "description": "Run health check to confirm recovery", "risk": "low"},
    ],
    "scale_replicas": [
        {"tool": "scale_replicas", "args": {"replicas": 3}, "description": "Scale replicas from 2 to 3 to absorb load", "risk": "low"},
        {"tool": "run_health_check", "args": {}, "description": "Run health check to confirm recovery", "risk": "low"},
    ],
}


def _integration_config(service: str) -> dict:
    """Configured-ness of each real integration (no network calls).

    "available" starts False and is filled in by run_investigation() once
    the agents have actually queried the integrations.
    """
    try:
        logs_provider = get_log_provider().provider_name
    except Exception:
        logs_provider = "none"
    try:
        dash = dashboard_url(service)
    except Exception:
        dash = None
    return {
        "prometheus": {"configured": bool((settings.prometheus_url or "").strip()), "available": False},
        "logs": {"provider": logs_provider, "available": False},
        "grafana": {"configured": bool((settings.grafana_url or "").strip()), "dashboard_url": dash},
    }


async def _fetch_real_logs(service: str, created_at) -> dict:
    """Query the configured real log provider over the last 60 minutes.

    Window ends at the incident's created_at when available, else now.
    Never raises: a failure is reported as available=False with an error,
    and the caller records an integration_unavailable evidence item.
    """
    end = created_at or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(minutes=60)
    try:
        provider_name = get_log_provider().provider_name
    except Exception:
        provider_name = "none"
    try:
        return await search_logs_real(service, start, end, level="error", limit=50)
    except Exception as exc:  # pragma: no cover - search_logs_real already guards
        # search_logs_real only converts LogProviderError into a result dict;
        # anything else (e.g. httpx.InvalidURL from proxy env parsing) still
        # escapes, so report it with the provider name attached.
        return {"provider": provider_name, "available": False, "entries": [], "groups": [],
                "error": f"log search failed: {exc}"}


async def _fetch_real_metrics(service: str) -> dict | None:
    """Query Prometheus for all five service metrics.

    Returns None when Prometheus is not configured. Per-metric failures are
    reported inside the returned dict (available=False) — never invented.
    """
    client = get_prometheus_client()
    if client is None:
        return None
    try:
        return await client.service_metrics(service)
    except Exception:
        return {name: {"available": False, "value": None, "unit": unit, "query": "",
                       "source": "prometheus", "error": "Prometheus query failed"}
                for name, unit in [("cpu", "%"), ("memory", "bytes"), ("error_rate", "%"),
                                   ("latency", "ms"), ("availability", "%")]}


def _event(db: Session, incident_id: str, type_: str, actor: str, payload: dict):
    db.add(IncidentEvent(incident_id=incident_id, type=type_, actor=actor, payload=payload))
    db.commit()


def _agent_run(db: Session, incident_id: str, agent: str, model: str, tokens: int, cost: float, latency_ms: int):
    db.add(AgentRun(incident_id=incident_id, agent=agent, prompt_version=PROMPT_VERSION, model=model, tokens=tokens, cost_usd=cost, latency_ms=latency_ms))
    db.commit()


async def _groq_hypothesis(incident: Incident, scenario: dict, evidence_text: str) -> dict | None:
    """Live LLM step. Returns parsed JSON, or {"_error": ...} on failure (caller falls back).

    The error is a short, sanitised HTTP/status message only — it never contains
    the API key. Previously this swallowed the error entirely, so a failing Groq
    key looked identical to a working one in the eval numbers.
    """
    client = GroqClient()
    if not client.available:
        return {"_error": "no Groq API key configured"}
    try:
        result = await client.chat_json(
            system="You are a site reliability engineer investigating a production incident. "
            "Use ONLY the evidence provided. Every claim must cite evidence. "
            "ABSTENTION RULES — follow these before proposing any root cause: "
            "(1) If the evidence does not name a specific root cause for THIS incident, "
            "abstain: set root_cause to \"\", confidence below 0.65, and tool to \"none\". "
            "Abstaining rather than guessing is the correct answer when evidence is insufficient. "
            "(2) Never invent deploy versions, service details, error messages, or root causes "
            "that do not appear in the evidence. "
            "(3) A runbook or postmortem describing a similar past incident is NOT evidence that "
            "this incident has the same cause; only logs or metrics from THIS incident can support "
            "a root cause. "
            "(4) When abstaining, reply with root_cause \"\", confidence below 0.65, tool \"none\". "
            "Reply with JSON: "
            '{"root_cause": str, "confidence": float 0-1, "tool": one of rollback_deploy|restart_service|scale_replicas|run_health_check|none, "reasoning": str}',
            user=f"Service: {incident.service}\nAlert: {incident.title}\nEvidence:\n{evidence_text}",
        )
        return {**result["data"], "_tokens": result["tokens"], "_cost": result["cost_usd"], "_latency": result["latency_ms"], "_model": result["model"]}
    except Exception as exc:
        # httpx error text carries status + URL, never the Authorization header.
        msg = str(exc).replace(client.api_key or "", "[redacted]")[:300]
        return {"_error": msg or "Groq request failed"}


def _deterministic_hypothesis(incident: Incident, scenario: dict, logs_out: dict, metrics_out: dict, kb_out: dict) -> dict:
    """Demo-mode reasoning: rank by explicit fixture signals, never invent."""
    if not scenario.get("root_cause"):
        return {"root_cause": "", "confidence": 0.0, "tool": "none", "abstain": True,
                "reasoning": "Insufficient evidence: error rate and logs are within baseline. Manual checks needed."}
    # Confidence from converging signals: error spike + log pattern + runbook match.
    signals = 0
    if metrics_out.get("anomaly"):
        signals += 1
    if logs_out.get("count", 0) >= 2:
        signals += 1
    if kb_out.get("results"):
        signals += 1
    confidence = 0.55 + 0.12 * signals  # 0.67–0.91, below threshold only if signals are weak
    return {"root_cause": scenario["root_cause"], "confidence": round(min(confidence, 0.93), 2),
            "tool": scenario.get("remediation_tool") or "none", "abstain": False,
            "reasoning": f"Error rate {metrics_out.get('error_rate')}% vs baseline {metrics_out.get('error_rate_baseline')}%, "
            f"{logs_out.get('count')} matching log lines, and a matching runbook/postmortem were found."}


async def run_investigation(db: Session, incident: Incident) -> dict:
    """Full supervisor loop. Returns a summary dict; persists evidence, hypotheses, plan, events.

    Evidence selection (per agent, in priority order):
    - Logs: real mode (demo_mode False) + a log provider configured -> search_logs_real
      over the last 60 minutes; provider down -> one "integration_unavailable"
      evidence item, no log evidence, never invented. Demo mode -> synthetic
      fixtures, labelled demo/synthetic. Real mode + no provider -> no log
      evidence, one "integration_unavailable" item.
    - Metrics: Prometheus configured -> service_metrics(), per-metric evidence
      (kind "metric", source "prometheus", value+unit+query, baseline note);
      a metric that is unavailable -> "integration_unavailable" for that
      metric, no invented value. Not configured -> synthetic metrics only in
      demo mode (labelled demo); in real mode an "integration_unavailable" item.
    - Knowledge: unchanged hybrid RAG (search_knowledge).
    A hypothesis is only proposed when usable evidence (everything except
    "integration_unavailable" items) exists and confidence >= 0.65.
    """
    started = time.time()
    scenario = get_scenario(incident.scenario or "checkout-oom")
    demo_mode = settings.demo_mode
    incident.status = "investigating"
    db.commit()
    integrations = _integration_config(incident.service)
    _event(db, incident.id, "agent", "supervisor", {"message": "Investigation started", "demo_mode": demo_mode,
            "model": "demo" if demo_mode else settings.groq_model, "integrations": integrations})

    # Parallel specialist agents (asyncio.gather = the LangGraph parallel branch).
    async def triage():
        t0 = time.time()
        # Service/severity come from the incident's own fields (its labels).
        return {"agent": "triage", "severity": incident.severity, "service": incident.service,
                "title": incident.title, "source": incident.source,
                "integrations_configured": {
                    "prometheus": integrations["prometheus"]["configured"],
                    "logs_provider": integrations["logs"]["provider"],
                    "grafana": integrations["grafana"]["configured"],
                    "grafana_dashboard_url": integrations["grafana"]["dashboard_url"],
                },
                "latency_ms": int((time.time() - t0) * 1000)}

    async def logs_agent():
        t0 = time.time()
        if not demo_mode:
            # Real mode: the configured provider is the only log source.
            result = await _fetch_real_logs(incident.service, incident.created_at)
            entries = result.get("entries") or []
            out = {"tool": "search_logs", "service": incident.service,
                   "count": len(entries),
                   "lines": [sanitise_evidence(e.get("message", "") if isinstance(e, dict) else str(e)) for e in entries],
                   "entries": entries, "groups": result.get("groups") or [],
                   "provider": result.get("provider", "none"),
                   "available": bool(result.get("available")),
                   "error": result.get("error"), "source": "real"}
            return {"agent": "logs", "out": out, "clusters": {}, "latency_ms": int((time.time() - t0) * 1000)}
        out = search_logs(SearchLogsInput(service=incident.service), scenario)
        clusters = get_error_clusters(SearchLogsInput(service=incident.service), scenario)
        out = {**out, "provider": integrations["logs"]["provider"], "available": False, "source": "demo"}
        return {"agent": "logs", "out": out, "clusters": clusters, "latency_ms": int((time.time() - t0) * 1000)}

    async def metrics_agent():
        t0 = time.time()
        prom_configured = integrations["prometheus"]["configured"]
        if not demo_mode:
            real = await _fetch_real_metrics(incident.service)
            baseline = SERVICE_STATE.get(incident.service, {})
            if real is None:
                out = {"tool": "get_metrics", "service": incident.service,
                       "error_rate": None, "error_rate_baseline": baseline.get("error_rate"),
                       "latency_ms": None, "latency_baseline": baseline.get("latency_ms"),
                       "cpu": None, "anomaly": False, "source": "real",
                       "configured": False, "metrics": {}}
                return {"agent": "metrics", "out": out, "latency_ms": int((time.time() - t0) * 1000)}
            er, lat, cpu = (real.get(k) or {} for k in ("error_rate", "latency", "cpu"))
            er_val = er.get("value") if er.get("available") else None
            lat_val = lat.get("value") if lat.get("available") else None
            cpu_val = cpu.get("value") if cpu.get("available") else None
            er_base = baseline.get("error_rate")
            out = {"tool": "get_metrics", "service": incident.service,
                   "error_rate": er_val, "error_rate_baseline": er_base,
                   "latency_ms": lat_val, "latency_baseline": baseline.get("latency_ms"),
                   "cpu": cpu_val,
                   "anomaly": bool(er_val is not None and er_base is not None and er_val > 5 * er_base),
                   "source": "real", "configured": True, "metrics": real}
            return {"agent": "metrics", "out": out, "latency_ms": int((time.time() - t0) * 1000)}
        out = get_metrics(MetricsInput(service=incident.service), scenario)
        out = {**out, "provider": "none", "source": "demo", "configured": prom_configured}
        return {"agent": "metrics", "out": out, "latency_ms": int((time.time() - t0) * 1000)}

    async def knowledge_agent():
        t0 = time.time()
        out = search_knowledge(KnowledgeInput(query=f"{incident.service} {incident.title}", service=incident.service))
        return {"agent": "knowledge", "out": out, "latency_ms": int((time.time() - t0) * 1000)}

    triage_out, logs_res, metrics_res, kb_out = await asyncio.gather(triage(), logs_agent(), metrics_agent(), knowledge_agent())
    lo, mo = logs_res["out"], metrics_res["out"]

    # Persist evidence (every claim must link here). "integration_unavailable"
    # items explain a gap; they are NOT usable evidence for a hypothesis.
    evidence_ids: list[str] = []
    usable_evidence_ids: list[str] = []

    def _add_evidence(kind: str, source_ref: str, content: str, usable: bool = True):
        ev = Evidence(incident_id=incident.id, kind=kind, source_ref=source_ref,
                      content=sanitise_evidence(content))
        db.add(ev); db.commit()
        evidence_ids.append(ev.id)
        if usable:
            usable_evidence_ids.append(ev.id)
        return ev

    if lo.get("source") == "real":
        if lo.get("available"):
            for entry in lo.get("entries", []):
                msg = entry.get("message", "")
                content = f"[{entry.get('timestamp', '')}] [{entry.get('level', '')}] {msg}"
                if entry.get("exception_type"):
                    content += f" (exception: {entry['exception_type']})"
                _add_evidence("log", lo.get("provider", "unknown"), content)
            for g in lo.get("groups", []):
                _add_evidence("log", lo.get("provider", "unknown"),
                              f"Exception {g.get('exception')!r} seen {g.get('count')} time(s) "
                              f"between {g.get('first_seen', '')} and {g.get('last_seen', '')}")
        else:
            provider = lo.get("provider", "none")
            detail = "no log provider configured" if provider == "none" else f"{provider} unavailable: {lo.get('error') or 'unknown error'}"
            _add_evidence("integration_unavailable", provider,
                          f"Real log search for {incident.service}: {detail}. No log evidence collected — logs were not invented.",
                          usable=False)
    else:
        for line in lo["lines"]:
            _add_evidence("log", f"{incident.service} logs (demo/synthetic)", line)

    if mo.get("source") == "real":
        if not mo.get("configured"):
            _add_evidence("integration_unavailable", "prometheus",
                          f"Prometheus is not configured (prometheus_url unset) — no real metrics collected for {incident.service}.",
                          usable=False)
        else:
            baseline = SERVICE_STATE.get(incident.service, {})
            for name, label in [("error_rate", "error_rate"), ("latency", "latency"), ("cpu", "cpu"),
                                ("memory", "memory"), ("availability", "availability")]:
                m = (mo.get("metrics") or {}).get(name) or {}
                if m.get("available"):
                    value, unit, query = m.get("value"), m.get("unit", ""), m.get("query", "")
                    content = f"{label} {value}{unit}"
                    if name == "error_rate" and baseline.get("error_rate") is not None:
                        content += (f" (baseline {baseline['error_rate']}%, "
                                    f"{'above baseline' if value > baseline['error_rate'] else 'at or below baseline'})")
                    if name == "latency" and baseline.get("latency_ms") is not None:
                        content += (f" (baseline {baseline['latency_ms']}ms, "
                                    f"{'above baseline' if value > baseline['latency_ms'] else 'at or below baseline'})")
                    if query:
                        content += f" — query: {query}"
                    _add_evidence("metric", "prometheus", content)
                else:
                    _add_evidence("integration_unavailable", "prometheus",
                                  f"Prometheus metric {label} unavailable for {incident.service}: "
                                  f"{m.get('error') or 'no data'}. No value invented.", usable=False)
    else:
        m = mo
        _add_evidence("metric", f"{incident.service} metrics (demo/synthetic)",
                      f"error_rate {m.get('error_rate')}% (baseline {m.get('error_rate_baseline')}%), "
                      f"latency {m.get('latency_ms')}ms (baseline {m.get('latency_baseline')}ms), cpu {m.get('cpu')}%")
    for doc in kb_out["out"]["results"]:
        _add_evidence("doc", doc["title"], doc["content"][:600])

    for name, out in [("triage", triage_out), ("logs", logs_res), ("metrics", metrics_res), ("knowledge", kb_out)]:
        _agent_run(db, incident.id, name, "demo" if demo_mode else settings.groq_model, 0, 0.0, out.get("latency_ms", 0))
        payload = {"message": f"{name} agent finished", "out": str(out)[:500]}
        if name == "triage":
            payload.update({"service": incident.service, "severity": incident.severity,
                            "integrations_configured": triage_out.get("integrations_configured")})
        _event(db, incident.id, "agent", name, payload)

    # What the integrations actually delivered (vs merely configured).
    integrations["logs"]["available"] = bool(lo.get("source") == "real" and lo.get("available"))
    integrations["prometheus"]["available"] = bool(
        mo.get("source") == "real" and mo.get("configured")
        and any((m or {}).get("available") for m in (mo.get("metrics") or {}).values()))

    # Related previous incidents: postmortems the Knowledge agent surfaced.
    related_previous = [{"title": d["title"], "kind": d.get("kind", "postmortem")}
                        for d in kb_out["out"].get("results", []) if d.get("kind") == "postmortem"]

    # Supervisor merge — live LLM if available, deterministic otherwise.
    evidence_text = "\n".join(e.content for e in db.query(Evidence).filter_by(incident_id=incident.id).all())
    llm = await _groq_hypothesis(incident, scenario, evidence_text) if not demo_mode else None
    tokens = cost = 0
    groq_error = (llm or {}).get("_error") if llm else None
    if groq_error:
        # Record the real failure reason on the incident timeline so a live
        # run can be diagnosed from the API instead of guessing from numbers.
        _event(db, incident.id, "groq_failed", "supervisor",
               {"message": "Groq LLM call failed — deterministic fallback used", "error": groq_error})
    if llm and llm.get("root_cause"):
        hyp = {"root_cause": llm["root_cause"], "confidence": float(llm.get("confidence", 0.0)),
               "tool": llm.get("tool", "none"), "abstain": float(llm.get("confidence", 0.0)) < ABSTAIN_CONFIDENCE,
               "reasoning": str(llm.get("reasoning", ""))}
        tokens, cost = int(llm.get("_tokens", 0)), float(llm.get("_cost", 0.0))
        _agent_run(db, incident.id, "supervisor", llm.get("_model", settings.groq_model), tokens, cost, int(llm.get("_latency", 0)))
    else:
        # Phase 8 guard — real-mode deterministic-fallback misattribution:
        # _deterministic_hypothesis reasons from a synthetic fixture keyed
        # by scenario. In live mode an ad-hoc/Alertmanager alert has no
        # known scenario (scenario "" — get_scenario falls back to the
        # checkout-oom fixture), so if Groq is down the deterministic path
        # would confidently blame a memory leak it never observed. For an
        # unknown scenario in live mode, abstain instead of guessing from
        # a fixture. Demo mode and explicitly simulated known scenarios
        # keep the deterministic path (labelled demo/synthetic).
        if not demo_mode and incident.scenario not in SCENARIOS:
            hyp = {"root_cause": "", "confidence": 0.0, "tool": "none", "abstain": True,
                   "reasoning": "Live LLM unavailable and this alert matches no known scenario — "
                   "deterministic demo reasoning must not be applied to a real alert. Manual triage required."}
        else:
            hyp = _deterministic_hypothesis(incident, scenario, lo, mo, kb_out["out"])
        _agent_run(db, incident.id, "supervisor", "demo", 0, 0.0, int((time.time() - started) * 1000))

    # Phase 5 abstention guard — deterministic, live mode only, LLM path
    # only. The LLM's own confidence is NOT trusted here: a confident
    # hallucination (e.g. a 0.95 "memory leak" for an unknown-signal alert)
    # must still abstain. The guard fires when usable evidence is only
    # knowledge-base docs (no log or metric evidence from this incident),
    # or when the scenario has no known root cause (unknown-signal, or an
    # ad-hoc alert with no scenario). It is lifted only when the proposed
    # root cause is directly supported by usable log/metric evidence —
    # see _root_cause_supported_by_observational_evidence. When it fires,
    # confidence is capped below the 0.65 threshold and the hypothesis is
    # forced to abstain. Demo mode and the deterministic fallback path are
    # untouched (llm is None there, so this block is skipped).
    if not demo_mode and llm and llm.get("root_cause") and not hyp.get("abstain"):
        usable = [e for e in db.query(Evidence).filter_by(incident_id=incident.id).all()
                  if e.id in set(usable_evidence_ids)]
        usable_kinds = {e.kind for e in usable}
        only_docs = bool(usable) and usable_kinds == {"doc"}
        unknown_cause = incident.scenario not in SCENARIOS or not scenario.get("root_cause")
        if only_docs or unknown_cause:
            obs_texts = [e.content for e in usable if e.kind in ("log", "metric")]
            proposed, proposed_conf = hyp.get("root_cause", ""), hyp.get("confidence", 0.0)
            if not _root_cause_supported_by_observational_evidence(proposed, obs_texts):
                reasons = []
                if only_docs:
                    reasons.append("usable evidence is only knowledge-base docs (no log or metric evidence from this incident)")
                if unknown_cause:
                    reasons.append("this scenario has no known root cause")
                hyp = {**hyp, "root_cause": "", "tool": "none", "abstain": True,
                       "confidence": min(proposed_conf, round(ABSTAIN_CONFIDENCE - 0.01, 2)),
                       "reasoning": ("Phase 5 abstention guard: LLM proposed "
                                     f"{proposed!r} at confidence {proposed_conf} but {'; '.join(reasons)} "
                                     "and no log/metric evidence directly supports it. Abstaining rather than guessing."),
                       "_guard": "phase5_abstention_guard"}

    # Enforce citation coverage: a hypothesis with no usable evidence, or
    # confidence below the threshold, is downgraded to abstain.
    if hyp.get("confidence", 0) < ABSTAIN_CONFIDENCE or not usable_evidence_ids or hyp.get("abstain"):
        incident.status = "abstained"
        db.add(Hypothesis(incident_id=incident.id, rank=1,
                          statement=hyp.get("root_cause") or "Insufficient evidence to determine a root cause.",
                          confidence=hyp.get("confidence", 0.0), status="abstained", evidence_ids=evidence_ids,
                          related_previous_incidents=related_previous,
                          alternatives=["Insufficient evidence — no alternative explanation can be assessed."],
                          next_steps=[f"Manual triage required for {incident.service}: check live logs and metrics; no automated remediation proposed."]))
        db.commit()
        _event(db, incident.id, "hypothesis", "supervisor", {"message": "Abstained — insufficient evidence", "confidence": hyp.get("confidence", 0.0),
                                                             "abstention_guard": hyp.get("_guard"),
                                                             "integrations": integrations})
        return {"incident_id": incident.id, "status": "abstained", "demo_mode": demo_mode,
                "integrations": integrations, "hypotheses": [], "tokens": tokens, "cost_usd": cost,
                "groq_error": groq_error, "abstention_guard": hyp.get("_guard")}

    # Remediation plan — only from the allow-listed tool the evidence supports.
    tool = hyp.get("tool") or "none"
    steps = PLAN_TEMPLATES.get(tool, [{"tool": "run_health_check", "args": {}, "description": "Run health check only — no remediation is safe to propose from this evidence", "risk": "low"}])
    # auth-cert special case: a restart does NOT fix an expired cert — never propose it.
    if incident.scenario == "auth-cert":
        steps = [{"tool": "rollback_deploy", "args": {}, "description": "Roll back the deploy that rotated the certificate, then escalate for certificate rotation", "risk": "medium"},
                 {"tool": "run_health_check", "args": {}, "description": "Run health check to confirm recovery", "risk": "low"}]
    next_steps = [s["description"] for s in steps]
    ambiguous = hyp["confidence"] < 0.85
    alt_statement = f"Alternative: transient load spike on {incident.service} (not supported by the deploy-correlated log pattern)."
    alternatives = [alt_statement] if ambiguous else ["No alternative explanation is supported by the current evidence."]

    h = Hypothesis(incident_id=incident.id, rank=1, statement=hyp["root_cause"], confidence=hyp["confidence"],
                   status="proposed", evidence_ids=evidence_ids,
                   related_previous_incidents=related_previous, alternatives=alternatives, next_steps=next_steps)
    db.add(h)
    # Second, lower-confidence alternative (shows the supervisor surfaces disagreement, not a single guess).
    db.add(Hypothesis(incident_id=incident.id, rank=2, statement=alt_statement,
                      confidence=round(max(0.2, hyp["confidence"] - 0.35), 2), status="alternative",
                      evidence_ids=evidence_ids[:2],
                      related_previous_incidents=related_previous, alternatives=[], next_steps=next_steps))
    db.commit()
    _event(db, incident.id, "hypothesis", "supervisor",
           {"message": hyp["root_cause"], "confidence": hyp["confidence"], "reasoning": hyp.get("reasoning", ""),
            "related_previous_incidents": related_previous, "alternatives": alternatives, "next_steps": next_steps})

    plan = RemediationPlan(
        incident_id=incident.id, steps=steps, risk="medium",
        rollback="Re-apply the previous deploy / restore previous replica count; re-run health check.",
        status="pending", expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.plan_expiry_minutes),
        approval_token=str(uuid.uuid4()),
    )
    db.add(plan); db.commit()
    incident.status = "awaiting_approval"
    db.commit()
    _event(db, incident.id, "hypothesis", "supervisor", {"message": "Remediation plan proposed — awaiting human approval", "plan_id": plan.id, "steps": steps})
    return {"incident_id": incident.id, "status": "awaiting_approval", "demo_mode": demo_mode,
            "integrations": integrations, "groq_error": groq_error,
            "hypotheses": [{"statement": h.statement, "confidence": h.confidence,
                            "evidence_ids": h.evidence_ids or [],
                            "related_previous_incidents": related_previous,
                            "alternatives": alternatives,
                            "next_steps": next_steps}],
            "plan_id": plan.id,
            "tokens": tokens, "cost_usd": cost, "latency_ms": int((time.time() - started) * 1000)}


def build_graph():
    """LangGraph StateGraph wiring the same node functions.

    Returns a compiled graph with a MemorySaver checkpointer (resumable
    investigations) or None if langgraph is unavailable — run_investigation()
    is the fallback and is what the API uses in both cases.
    """
    try:
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, StateGraph
        from typing import TypedDict

        class State(TypedDict, total=False):
            incident_id: str
            step: str

        graph = StateGraph(State)

        async def triage_node(state):
            return {"step": "triage done"}

        async def investigate_node(state):
            return {"step": "investigation done"}

        graph.add_node("triage", triage_node)
        graph.add_node("investigate", investigate_node)
        graph.set_entry_point("triage")
        graph.add_edge("triage", "investigate")
        graph.add_edge("investigate", END)
        return graph.compile(checkpointer=MemorySaver())
    except Exception:
        return None
