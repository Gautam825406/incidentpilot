"""IncidentPilot API — FastAPI. PRD §10.

Routes: /api/health, /api/auth/login, /api/webhooks/alert, /api/simulate,
/api/incidents (+ detail/investigate/stream), /api/plans/{id}/approve|reject|execute,
/api/documents, /api/knowledge/search, /api/evals/run, /api/evals/results,
/api/scenarios. Auth is required for approve/reject/execute; reads are open
in the demo so a recruiter can browse without an account.
"""
import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .agents import build_graph, run_investigation
from .config import settings
from .database import SessionLocal, get_db, init_db
from .executor import execute_plan, postmortem_draft, verify_recovery
from .integrations.grafana import dashboard_url, grafana_status
from .integrations.logs import get_log_provider
from .integrations.prometheus import PrometheusClient
from .llm import hash_embedding
from .models import AgentRun, Document, EvalRun, Evidence, Hypothesis, Incident, IncidentEvent, RemediationPlan, User
from .schemas import AlertIn, AlertmanagerPayload, DocumentIn, ExecuteIn, LoginIn, PlanEditIn, RejectIn, SimulateIn, incident_out
from .security import create_token, verify_password, verify_token
from .seed import seed
from .synthetic import SERVICE_STATE, get_scenario, list_scenarios
from .tools import KnowledgeInput, search_knowledge

app = FastAPI(title="IncidentPilot", version="1.0.0",
              description="Multi-agent production incident resolver — LangGraph + Groq (demo mode without a key).")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
_graph = build_graph()  # LangGraph graph with MemorySaver checkpointer (None if langgraph unavailable)

init_db()
with SessionLocal() as _db:
    seed(_db)


def _event(db: Session, incident_id: str, type_: str, actor: str, payload: dict):
    db.add(IncidentEvent(incident_id=incident_id, type=type_, actor=actor, payload=payload))
    db.commit()


def _require_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token — log in first")
    data = verify_token(authorization[7:])
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return data


def _require_role(roles: set[str]):
    def _dep(user: dict = Depends(_require_user)):
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail=f"Role {user.get('role')} may not do this")
        return user
    return _dep


def _detail(db: Session, incident: Incident) -> dict:
    hyps = db.query(Hypothesis).filter_by(incident_id=incident.id).order_by(Hypothesis.rank).all()
    evs = db.query(Evidence).filter_by(incident_id=incident.id).all()
    events = db.query(IncidentEvent).filter_by(incident_id=incident.id).order_by(IncidentEvent.created_at).all()
    plans = db.query(RemediationPlan).filter_by(incident_id=incident.id).order_by(RemediationPlan.created_at.desc()).all()
    runs = db.query(AgentRun).filter_by(incident_id=incident.id).all()
    plan = plans[0] if plans else None
    # Monitoring links/config for this incident's service. Guarded so a
    # failing integration helper can never break the detail response.
    try:
        monitoring = {
            "grafana_dashboard_url": dashboard_url(incident.service),
            "prometheus_configured": bool((settings.prometheus_url or "").strip()),
            "logs_provider": get_log_provider().provider_name,
        }
    except Exception:
        monitoring = {"grafana_dashboard_url": None, "prometheus_configured": False, "logs_provider": "none"}
    return {
        **incident_out(incident),
        "demo_mode": settings.demo_mode,
        "monitoring": monitoring,
        "hypotheses": [{"id": h.id, "rank": h.rank, "statement": h.statement, "confidence": h.confidence, "status": h.status, "evidence_ids": h.evidence_ids or [],
                        "related_previous_incidents": h.related_previous_incidents or [],
                        "alternatives": h.alternatives or [], "next_steps": h.next_steps or []} for h in hyps],
        "evidence": [{"id": e.id, "kind": e.kind, "source_ref": e.source_ref, "content": e.content} for e in evs],
        "events": [{"id": e.id, "type": e.type, "actor": e.actor, "payload": e.payload, "created_at": e.created_at.isoformat() if e.created_at else None} for e in events],
        "plan": {"id": plan.id, "steps": plan.steps, "risk": plan.risk, "rollback": plan.rollback, "status": plan.status,
                 "approved_by": plan.approved_by, "expires_at": plan.expires_at.isoformat() if plan.expires_at else None} if plan else None,
        "agent_runs": [{"agent": r.agent, "model": r.model, "tokens": r.tokens, "cost_usd": r.cost_usd, "latency_ms": r.latency_ms} for r in runs],
        "total_tokens": sum(r.tokens for r in runs),
        "total_cost_usd": round(sum(r.cost_usd for r in runs), 6),
        "postmortem": postmortem_draft(incident, hyps, plan) if incident.status in ("resolved", "verifying", "approved", "awaiting_approval") and hyps else None,
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "demo_mode": settings.demo_mode, "model": "demo" if settings.demo_mode else settings.groq_model,
            "langgraph": _graph is not None}


@app.get("/api/scenarios")
def scenarios():
    return {"scenarios": list_scenarios(), "demo_mode": settings.demo_mode}


@app.post("/api/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=body.email).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid login credentials")
    return {"token": create_token(user.id, user.email, user.role), "user": {"name": user.name, "email": user.email, "role": user.role}}


@app.post("/api/webhooks/alert")
async def webhook(request: Request, db: Session = Depends(get_db)):
    """Accept both shapes (Phase 3):
    - the original AlertIn JSON {title, service, severity, source, external_id}
      — behaviour unchanged from v1;
    - a Prometheus Alertmanager payload, detected by the "alerts" key.

    Optional shared-secret auth (Phase 8): when ALERT_WEBHOOK_SECRET is
    set, callers must send it as header ``X-Alert-Token`` or the webhook
    returns 401. When unset (the demo default) the webhook stays open so
    Alertmanager needs no extra configuration.
    """
    secret = (settings.alert_webhook_secret or "").strip()
    if secret and not hmac.compare_digest(request.headers.get("x-alert-token", ""), secret):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Alert-Token header")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid alert payload: body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid alert payload: body must be a JSON object")
    if "alerts" in payload:
        return await _webhook_alertmanager(payload, db)
    try:
        body = AlertIn.model_validate(payload)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid alert payload: title and service are required")
    ext = body.external_id or f"alert-{uuid.uuid4().hex[:8]}"
    existing = db.query(Incident).filter_by(external_id=ext).first()
    if existing:
        return {"incident_id": existing.id, "deduplicated": True, **incident_out(existing)}
    inc = Incident(title=body.title, service=body.service, severity=body.severity, source=body.source, external_id=ext)
    db.add(inc); db.commit(); db.refresh(inc)
    _event(db, inc.id, "alert", body.source, {"title": body.title, "service": body.service, "severity": body.severity})
    asyncio.create_task(_run_bg(inc.id))
    return {"incident_id": inc.id, "deduplicated": False, **incident_out(inc)}


_SEVERITY_MAP = {
    "critical": "P1", "page": "P1", "p1": "P1",
    "warning": "P2", "major": "P2", "high": "P2", "p2": "P2",
    "info": "P3", "minor": "P3", "low": "P3", "p3": "P3",
}


def _am_severity(labels: dict) -> str:
    return _SEVERITY_MAP.get(str(labels.get("severity", "")).strip().lower(), "P3")


def _am_external_id(alert) -> str:
    if alert.fingerprint:
        return str(alert.fingerprint)
    digest = hashlib.sha256(json.dumps(
        {"labels": alert.labels, "annotations": alert.annotations, "startsAt": alert.startsAt},
        sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f"am-{digest}"


async def _webhook_alertmanager(payload: dict, db: Session) -> dict:
    try:
        body = AlertmanagerPayload.model_validate(payload)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid Alertmanager payload: alerts must be a list of alert objects")
    if not body.alerts:
        raise HTTPException(status_code=422, detail="Invalid Alertmanager payload: alerts list is empty")
    created_ids: list[str] = []
    dedup_ids: list[str] = []
    skipped_resolved = 0
    for alert in body.alerts:
        if str(alert.status).lower() == "resolved":
            skipped_resolved += 1
            continue
        labels = dict(body.commonLabels or {})
        labels.update(alert.labels or {})
        annotations = dict(body.commonAnnotations or {})
        annotations.update(alert.annotations or {})
        service = labels.get("service") or labels.get("job") or labels.get("instance") or "unknown"
        severity = _am_severity(labels)
        title = annotations.get("summary") or labels.get("alertname") or "Alertmanager alert"
        description = annotations.get("description") or annotations.get("summary") or ""
        ext = _am_external_id(alert)
        existing = db.query(Incident).filter_by(external_id=ext).first()
        if existing and existing.status != "resolved":
            dedup_ids.append(existing.id)
            continue
        if existing and existing.status == "resolved":
            # external_id is unique: a re-firing alert for a resolved incident
            # gets a fresh id so the new incident can be created.
            ext = f"{ext}-{uuid.uuid4().hex[:8]}"
        inc = Incident(title=title, service=service, severity=severity,
                       source="alertmanager", external_id=ext, scenario="")
        db.add(inc); db.commit(); db.refresh(inc)
        _event(db, inc.id, "alert", "alertmanager",
               {"title": title, "service": service, "severity": severity,
                "alertname": labels.get("alertname", ""), "description": description,
                "fingerprint": alert.fingerprint or ""})
        asyncio.create_task(_run_bg(inc.id))
        created_ids.append(inc.id)
    if created_ids:
        return {"acknowledged": True, "deduplicated": False, "incident_id": created_ids[0],
                "incident_ids": created_ids, "deduplicated_ids": dedup_ids,
                "skipped_resolved": skipped_resolved}
    if dedup_ids:
        return {"acknowledged": True, "deduplicated": True, "incident_id": dedup_ids[0],
                "incident_ids": [], "deduplicated_ids": dedup_ids,
                "skipped_resolved": skipped_resolved}
    return {"acknowledged": True, "deduplicated": False, "incident_id": None,
            "incident_ids": [], "deduplicated_ids": [],
            "skipped_resolved": skipped_resolved}


@app.get("/api/integrations/status")
async def integrations_status():
    """Phase 2/3/4 status: Prometheus (live health check when configured),
    logs (configured provider, via get_log_provider), Grafana links.
    No tokens or secrets are ever included in this response."""
    prom_url = (settings.prometheus_url or "").strip() or None
    prom_configured = bool(prom_url)
    prom_reachable: bool | None = None
    if prom_configured:
        # Live health check, capped at a short timeout so /status stays fast.
        client = PrometheusClient(timeout=min(settings.prometheus_timeout, 5.0))
        try:
            prom_reachable = (await client.health())["reachable"]
        except Exception:
            prom_reachable = False
    try:
        logs_provider = get_log_provider().provider_name
    except Exception:
        logs_provider = "none"
    return {
        "prometheus": {"configured": prom_configured, "reachable": prom_reachable, "url": prom_url},
        "logs": {"provider": logs_provider, "configured": logs_provider != "none"},
        "grafana": grafana_status(),
    }


async def _run_bg(incident_id: str):
    db = SessionLocal()
    try:
        inc = db.query(Incident).filter_by(id=incident_id).first()
        if inc:
            await run_investigation(db, inc)
    finally:
        db.close()


@app.post("/api/simulate")
async def simulate(body: SimulateIn, db: Session = Depends(get_db)):
    sc = get_scenario(body.scenario)
    inc = Incident(title=sc["title"], service=sc["service"], severity=sc["severity"], source="simulator",
                   external_id=f"sim-{uuid.uuid4().hex[:8]}", scenario=body.scenario)
    db.add(inc); db.commit(); db.refresh(inc)
    SERVICE_STATE.setdefault(sc["service"], {})["error_rate"] = sc["metrics"]["error_rate"]
    _event(db, inc.id, "alert", "simulator", {"title": sc["title"], "scenario": body.scenario})
    result = await run_investigation(db, inc)
    return {"incident_id": inc.id, "investigation": result, **incident_out(inc)}


@app.get("/api/incidents")
def list_incidents(status: str | None = None, service: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Incident).order_by(Incident.opened_at.desc())
    if status:
        q = q.filter_by(status=status)
    if service:
        q = q.filter_by(service=service)
    return {"incidents": [incident_out(i) for i in q.all()], "demo_mode": settings.demo_mode}


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str, db: Session = Depends(get_db)):
    inc = db.query(Incident).filter_by(id=incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    return _detail(db, inc)


@app.post("/api/incidents/{incident_id}/investigate")
async def investigate(incident_id: str, db: Session = Depends(get_db)):
    inc = db.query(Incident).filter_by(id=incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    return await run_investigation(db, inc)


@app.get("/api/incidents/{incident_id}/stream")
async def stream(incident_id: str, db: Session = Depends(get_db)):
    inc = db.query(Incident).filter_by(id=incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    events = db.query(IncidentEvent).filter_by(incident_id=incident_id).order_by(IncidentEvent.created_at).all()

    async def gen():
        for e in events:
            yield f"data: {json.dumps({'type': e.type, 'actor': e.actor, 'payload': e.payload})}\n\n"
            await asyncio.sleep(0.05)
        yield f"data: {json.dumps({'type': 'done', 'status': inc.status})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/plans/{plan_id}/approve")
def approve(plan_id: str, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    plan = db.query(RemediationPlan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if plan.status != "pending":
        raise HTTPException(status_code=400, detail=f"Plan is {plan.status}, cannot approve")
    plan.status = "approved"; plan.approved_by = user["email"]; plan.approved_at = datetime.now(timezone.utc)
    db.commit()
    _event(db, plan.incident_id, "approval", user["email"], {"message": f"Plan approved by {user['email']}", "plan_id": plan.id})
    inc = db.query(Incident).filter_by(id=plan.incident_id).first()
    if inc:
        inc.status = "approved"; db.commit()
    return {"ok": True, "plan_id": plan.id, "status": "approved", "approval_token": plan.approval_token}


@app.post("/api/plans/{plan_id}/reject")
def reject(plan_id: str, body: RejectIn, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    plan = db.query(RemediationPlan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan.status = "rejected"; db.commit()
    _event(db, plan.incident_id, "approval", user["email"], {"message": f"Plan rejected: {body.reason}", "plan_id": plan.id})
    return {"ok": True, "plan_id": plan.id, "status": "rejected"}


@app.post("/api/plans/{plan_id}/edit")
def edit_plan(plan_id: str, body: PlanEditIn, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    plan = db.query(RemediationPlan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if plan.status not in ("pending", "draft"):
        raise HTTPException(status_code=400, detail=f"Plan is {plan.status}, cannot edit")
    plan.steps = body.steps; db.commit()
    _event(db, plan.incident_id, "approval", user["email"], {"message": "Plan edited before approval", "plan_id": plan.id})
    return {"ok": True, "plan_id": plan.id, "steps": plan.steps}


@app.post("/api/plans/{plan_id}/execute")
def execute(plan_id: str, body: ExecuteIn, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    plan = db.query(RemediationPlan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if body.approval_token and body.approval_token != plan.approval_token:
        raise HTTPException(status_code=403, detail="Invalid approval token")
    inc = db.query(Incident).filter_by(id=plan.incident_id).first()
    result = execute_plan(db, plan, inc, dry_run=body.dry_run, executed_by=user["email"])
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Execution refused"))
    return result


@app.post("/api/incidents/{incident_id}/verify")
def verify(incident_id: str, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    inc = db.query(Incident).filter_by(id=incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    return verify_recovery(db, inc)


@app.get("/api/documents")
def list_docs(db: Session = Depends(get_db)):
    return {"documents": [{"id": d.id, "title": d.title, "kind": d.kind, "service": d.service} for d in db.query(Document).all()]}


@app.post("/api/documents")
def add_doc(body: DocumentIn, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    doc = Document(title=body.title, kind=body.kind, service=body.service, content=body.content, embedding=hash_embedding(body.content))
    db.add(doc); db.commit()
    return {"id": doc.id, "title": doc.title}


@app.post("/api/knowledge/search")
def knowledge(body: dict, db: Session = Depends(get_db)):
    return search_knowledge(KnowledgeInput(query=body.get("query", ""), service=body.get("service", "")))


@app.get("/api/evals/results")
def eval_results(db: Session = Depends(get_db)):
    runs = db.query(EvalRun).order_by(EvalRun.run_at.desc()).limit(10).all()
    return {"runs": [{"id": r.id, "dataset_version": r.dataset_version, "model": r.model, "metrics": r.metrics, "run_at": r.run_at.isoformat() if r.run_at else None} for r in runs]}


@app.post("/api/evals/run")
async def run_eval(db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin"}))):
    from evals.run_eval import run_all
    metrics = await run_all(db)
    return {"metrics": metrics}
