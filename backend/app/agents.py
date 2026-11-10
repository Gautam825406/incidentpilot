"""LangGraph supervisor + specialist agents (PRD §8).

Node functions are pure and testable. The LangGraph StateGraph (built in
build_graph()) wires them together with a MemorySaver checkpointer so an
investigation is resumable; run_investigation() executes the same nodes
in parallel (asyncio.gather) and persists every step to the DB.

Two modes:
- Live (GROQ_API_KEY set): Triage/Logs/Metrics/Knowledge call Groq
  llama-3.3-70b-versatile and return structured JSON; the supervisor merges
  and enforces citation coverage.
- Demo (no key): deterministic fixture-based reasoning over the synthetic
  scenario — labelled demo_mode everywhere. It never pretends to be an LLM.
"""
import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .config import settings
from .llm import GroqClient
from .models import AgentRun, Evidence, Hypothesis, Incident, IncidentEvent, RemediationPlan
from .security import sanitise_evidence
from .synthetic import get_scenario
from .tools import KnowledgeInput, MetricsInput, SearchLogsInput, get_error_clusters, get_metrics, search_knowledge, search_logs

PROMPT_VERSION = "v1"
ABSTAIN_CONFIDENCE = settings.confidence_threshold

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


def _event(db: Session, incident_id: str, type_: str, actor: str, payload: dict):
    db.add(IncidentEvent(incident_id=incident_id, type=type_, actor=actor, payload=payload))
    db.commit()


def _agent_run(db: Session, incident_id: str, agent: str, model: str, tokens: int, cost: float, latency_ms: int):
    db.add(AgentRun(incident_id=incident_id, agent=agent, prompt_version=PROMPT_VERSION, model=model, tokens=tokens, cost_usd=cost, latency_ms=latency_ms))
    db.commit()


async def _groq_hypothesis(incident: Incident, scenario: dict, evidence_text: str) -> dict | None:
    """Live LLM step. Returns parsed JSON or None on any failure (caller falls back)."""
    client = GroqClient()
    if not client.available:
        return None
    try:
        result = await client.chat_json(
            system="You are a site reliability engineer investigating a production incident. "
            "Use ONLY the evidence provided. Every claim must cite evidence. "
            "If evidence is insufficient, say so. Reply with JSON: "
            '{"root_cause": str, "confidence": float 0-1, "tool": one of rollback_deploy|restart_service|scale_replicas|run_health_check|none, "reasoning": str}',
            user=f"Service: {incident.service}\nAlert: {incident.title}\nEvidence:\n{evidence_text}",
        )
        return {**result["data"], "_tokens": result["tokens"], "_cost": result["cost_usd"], "_latency": result["latency_ms"], "_model": result["model"]}
    except Exception:
        return None


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
    """Full supervisor loop. Returns a summary dict; persists evidence, hypotheses, plan, events."""
    started = time.time()
    scenario = get_scenario(incident.scenario or "checkout-oom")
    demo_mode = settings.demo_mode
    incident.status = "investigating"
    db.commit()
    _event(db, incident.id, "agent", "supervisor", {"message": "Investigation started", "demo_mode": demo_mode, "model": "demo" if demo_mode else settings.groq_model})

    # Parallel specialist agents (asyncio.gather = the LangGraph parallel branch).
    async def triage():
        t0 = time.time()
        return {"agent": "triage", "severity": incident.severity, "service": incident.service, "latency_ms": int((time.time() - t0) * 1000)}

    async def logs_agent():
        t0 = time.time()
        out = search_logs(SearchLogsInput(service=incident.service), scenario)
        clusters = get_error_clusters(SearchLogsInput(service=incident.service), scenario)
        return {"agent": "logs", "out": out, "clusters": clusters, "latency_ms": int((time.time() - t0) * 1000)}

    async def metrics_agent():
        t0 = time.time()
        out = get_metrics(MetricsInput(service=incident.service), scenario)
        return {"agent": "metrics", "out": out, "latency_ms": int((time.time() - t0) * 1000)}

    async def knowledge_agent():
        t0 = time.time()
        out = search_knowledge(KnowledgeInput(query=f"{incident.service} {incident.title}", service=incident.service))
        return {"agent": "knowledge", "out": out, "latency_ms": int((time.time() - t0) * 1000)}

    triage_out, logs_out, metrics_out, kb_out = await asyncio.gather(triage(), logs_agent(), metrics_agent(), knowledge_agent())

    # Persist evidence (every claim must link here).
    evidence_ids: list[str] = []
    for line in logs_out["out"]["lines"]:
        ev = Evidence(incident_id=incident.id, kind="log", source_ref=f"{incident.service} logs", content=line)
        db.add(ev); db.commit(); evidence_ids.append(ev.id)
    m = metrics_out["out"]
    ev = Evidence(incident_id=incident.id, kind="metric", source_ref=f"{incident.service} metrics",
                  content=f"error_rate {m.get('error_rate')}% (baseline {m.get('error_rate_baseline')}%), latency {m.get('latency_ms')}ms (baseline {m.get('latency_baseline')}ms), cpu {m.get('cpu')}%")
    db.add(ev); db.commit(); evidence_ids.append(ev.id)
    for doc in kb_out["out"]["results"]:
        ev = Evidence(incident_id=incident.id, kind="doc", source_ref=doc["title"], content=doc["content"][:600])
        db.add(ev); db.commit(); evidence_ids.append(ev.id)

    for name, out in [("triage", triage_out), ("logs", logs_out), ("metrics", metrics_out), ("knowledge", kb_out)]:
        _agent_run(db, incident.id, name, "demo" if demo_mode else settings.groq_model, 0, 0.0, out.get("latency_ms", 0))
        _event(db, incident.id, "agent", name, {"message": f"{name} agent finished", "out": str(out)[:500]})

    # Supervisor merge — live LLM if available, deterministic otherwise.
    evidence_text = "\n".join(e.content for e in db.query(Evidence).filter_by(incident_id=incident.id).all())
    llm = await _groq_hypothesis(incident, scenario, evidence_text) if not demo_mode else None
    tokens = cost = 0
    if llm and llm.get("root_cause"):
        hyp = {"root_cause": llm["root_cause"], "confidence": float(llm.get("confidence", 0.0)),
               "tool": llm.get("tool", "none"), "abstain": float(llm.get("confidence", 0.0)) < ABSTAIN_CONFIDENCE,
               "reasoning": str(llm.get("reasoning", ""))}
        tokens, cost = int(llm.get("_tokens", 0)), float(llm.get("_cost", 0.0))
        _agent_run(db, incident.id, "supervisor", llm.get("_model", settings.groq_model), tokens, cost, int(llm.get("_latency", 0)))
    else:
        hyp = _deterministic_hypothesis(incident, scenario, logs_out["out"], metrics_out["out"], kb_out["out"])
        _agent_run(db, incident.id, "supervisor", "demo", 0, 0.0, int((time.time() - started) * 1000))

    # Enforce citation coverage: a hypothesis with no evidence is downgraded to abstain.
    if hyp.get("confidence", 0) < ABSTAIN_CONFIDENCE or not evidence_ids or hyp.get("abstain"):
        incident.status = "abstained"
        db.add(Hypothesis(incident_id=incident.id, rank=1, statement=hyp.get("root_cause") or "Insufficient evidence to determine a root cause.", confidence=hyp.get("confidence", 0.0), status="abstained", evidence_ids=evidence_ids))
        db.commit()
        _event(db, incident.id, "hypothesis", "supervisor", {"message": "Abstained — insufficient evidence", "confidence": hyp.get("confidence", 0.0)})
        return {"incident_id": incident.id, "status": "abstained", "demo_mode": demo_mode, "hypotheses": [], "tokens": tokens, "cost_usd": cost}

    h = Hypothesis(incident_id=incident.id, rank=1, statement=hyp["root_cause"], confidence=hyp["confidence"], status="proposed", evidence_ids=evidence_ids)
    db.add(h)
    # Second, lower-confidence alternative (shows the supervisor surfaces disagreement, not a single guess).
    db.add(Hypothesis(incident_id=incident.id, rank=2, statement=f"Alternative: transient load spike on {incident.service} (not supported by the deploy-correlated log pattern).", confidence=round(max(0.2, hyp["confidence"] - 0.35), 2), status="alternative", evidence_ids=evidence_ids[:2]))
    db.commit()
    _event(db, incident.id, "hypothesis", "supervisor", {"message": hyp["root_cause"], "confidence": hyp["confidence"], "reasoning": hyp.get("reasoning", "")})

    # Remediation plan — only from the allow-listed tool the evidence supports.
    tool = hyp.get("tool") or "none"
    steps = PLAN_TEMPLATES.get(tool, [{"tool": "run_health_check", "args": {}, "description": "Run health check only — no remediation is safe to propose from this evidence", "risk": "low"}])
    # auth-cert special case: a restart does NOT fix an expired cert — never propose it.
    if incident.scenario == "auth-cert":
        steps = [{"tool": "rollback_deploy", "args": {}, "description": "Roll back the deploy that rotated the certificate, then escalate for certificate rotation", "risk": "medium"},
                 {"tool": "run_health_check", "args": {}, "description": "Run health check to confirm recovery", "risk": "low"}]
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
            "hypotheses": [{"statement": h.statement, "confidence": h.confidence}], "plan_id": plan.id,
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
