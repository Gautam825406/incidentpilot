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
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .agents import build_graph, run_investigation
from .config import settings, validate_production_settings
from .database import SessionLocal, get_db, init_db
from .executor import execute_plan, postmortem_draft, verify_recovery
from .integrations.grafana import dashboard_url, grafana_status
from .integrations.jira import JiraClient, JiraError, jira_status
from .integrations.logs import get_log_provider
from .integrations.prometheus import PrometheusClient, get_prometheus_client
from .integrations.slack import SlackClient, SlackError, slack_status
from .llm import hash_embedding
from .telemetry import emit_scenario_telemetry, store as telemetry_store
from .models import AgentRun, Document, EvalRun, Evidence, Hypothesis, Incident, IncidentEvent, RemediationPlan, User
from .rate_limit import RateLimiter
from .schemas import AlertIn, AlertmanagerPayload, DocumentIn, ExecuteIn, KnowledgeSearchIn, LoginIn, NotifyIn, PlanEditIn, RejectIn, SimulateIn, incident_out
from .security import create_token, hash_password, is_legacy_hash, redact_pii, verify_password, verify_token
from .seed import seed
from .synthetic import SERVICE_STATE, get_scenario, list_scenarios
from .tools import KnowledgeInput, search_knowledge
from .worker import enqueue_investigation, notify_slack_best_effort, worker

logger = logging.getLogger(__name__)

# Fail loudly at startup if a live deployment still uses the demo JWT
# secret. Env-driven and import-time on purpose: production settings come
# from the environment, fixed before the app starts (see config.py).
validate_production_settings(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker.start()  # in-process investigation queue (Phase 4)
    yield


app = FastAPI(title="IncidentPilot", version="1.0.0",
              description="Multi-agent production incident resolver — LangGraph + Groq (demo mode without a key).",
              lifespan=lifespan)


def _cors_allow_credentials(origins: list[str]) -> bool:
    """Credentials (Authorization header / cookies) must never be sent
    with a wildcard origin: browsers ignore credentials on ``*`` and a
    reflected-origin setup would hand the demo JWT to any site. Only an
    explicit allow-list gets credentials."""
    return "*" not in origins


# CORS is an env-configured allow-list (Phase 4); "*" only when explicitly
# set via CORS_ORIGINS. See config.py cors_origin_list. Credentials are
# disabled when the list is "*" (see _cors_allow_credentials).
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list,
                   allow_credentials=_cors_allow_credentials(settings.cors_origin_list),
                   allow_methods=["*"], allow_headers=["*"])
if "*" in settings.cors_origin_list:
    logger.warning("CORS_ORIGINS is '*' — credentials are disabled and any origin may call the API. "
                   "Set an explicit allow-list for a shared deployment.")


# Request-body size cap (security review): no legitimate request here is
# large (largest is a runbook at 100k), so a 1 MB Content-Length cap
# blocks body-flood DoS without touching normal use. Requests without a
# Content-Length header (chunked) are not capped by this check — the
# per-field Pydantic limits still bound what is stored.
MAX_REQUEST_BODY_BYTES = 1_000_000


@app.middleware("http")
async def _limit_request_body_size(request: Request, call_next):
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(status_code=413, content={"detail": "Request body too large"})
        except ValueError:
            pass
    return await call_next(request)
_graph = build_graph()  # LangGraph graph with MemorySaver checkpointer (None if langgraph unavailable)

init_db()
with SessionLocal() as _db:
    seed(_db)


# --- Rate limiting (Phase 4, in-memory sliding window) ----------------------
# Limits are read from settings at construction; tests lower them via
# `login_limiter.limit = N` / reset state via `login_limiter.reset()`
# (see tests/conftest.py — the limiter stays ON in tests, reset per test).
# Disable entirely with RATE_LIMIT_ENABLED=false. The 429 message is
# deliberately generic: it reveals nothing about accounts or internals.
login_limiter = RateLimiter(limit=settings.rate_limit_login_per_minute)
webhook_limiter = RateLimiter(limit=settings.rate_limit_webhook_per_minute)
# LLM-cost endpoints: /api/simulate, /api/incidents/{id}/investigate and
# /api/evals/run each trigger Groq calls. They stay open to demo users
# (no auth on simulate/investigate by design), but per-IP rate limiting
# caps unauthenticated cost abuse. Same reset hook as the other limiters
# (tests/conftest.py).
investigate_limiter = RateLimiter(limit=settings.rate_limit_investigate_per_minute)


def _client_ip(request: Request) -> str:
    return getattr(request.client, "host", None) or "unknown"


def _rate_limit_login(request: Request):
    if settings.rate_limit_enabled and not login_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many login attempts — try again later")


def _rate_limit_webhook(request: Request):
    if settings.rate_limit_enabled and not webhook_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many alert requests — try again later")


def _rate_limit_investigate(request: Request):
    if settings.rate_limit_enabled and not investigate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many investigation requests — try again later")


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


@app.get("/metrics")
def metrics():
    """Prometheus scrape endpoint (Phase 6).

    Unauthenticated, like /api/health, so Prometheus can scrape it with no
    credentials. It exposes ONLY aggregate counters/gauges computed from
    the simulated services' emitted telemetry (request counts, latency
    histogram, CPU) — no incident content, no log lines, no secrets, and
    no memory values (memory is not measured by the simulator).
    """
    return PlainTextResponse(telemetry_store.prometheus_text(),
                             media_type="text/plain; version=0.0.4")


@app.get("/api/scenarios")
def scenarios():
    return {"scenarios": list_scenarios(), "demo_mode": settings.demo_mode}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db),
          _limited: None = Depends(_rate_limit_login)):
    user = db.query(User).filter_by(email=body.email).first()
    if not user or not verify_password(body.password, user.password_hash):
        # Audit log (never the password): failed logins are the signal
        # for credential stuffing. The 401 message stays generic so it
        # does not reveal whether the email exists.
        logger.warning("Failed login attempt for %s from %s", body.email, _client_ip(request))
        raise HTTPException(status_code=401, detail="Invalid login credentials")
    if is_legacy_hash(user.password_hash):
        # Opportunistic upgrade: a pre–Phase 4 fixed-salt hash is
        # re-hashed with a fresh per-user random salt on successful
        # login, so legacy hashes disappear without a password reset.
        # Demo logins keep working — the old hash verified just now.
        user.password_hash = hash_password(body.password)
        db.commit()
        logger.info("Upgraded legacy password hash for %s", user.email)
    logger.info("Login succeeded for %s (%s) from %s", user.email, user.role, _client_ip(request))
    return {"token": create_token(user.id, user.email, user.role), "user": {"name": user.name, "email": user.email, "role": user.role}}


@app.post("/api/webhooks/alert")
async def webhook(request: Request, db: Session = Depends(get_db),
                  _limited: None = Depends(_rate_limit_webhook)):
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
    _event(db, inc.id, "alert", body.source, {"title": redact_pii(body.title), "service": body.service, "severity": body.severity})
    enqueue_investigation(inc.id)  # background worker queue (Phase 4)
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
        # Alert annotations are untrusted external text — redact PII /
        # secrets before they land in the audit trail (the incident
        # title itself is kept as-is for display; the event payload is
        # the copy that gets redacted).
        _event(db, inc.id, "alert", "alertmanager",
               {"title": redact_pii(title), "service": service, "severity": severity,
                "alertname": labels.get("alertname", ""), "description": redact_pii(description),
                "fingerprint": alert.fingerprint or ""})
        enqueue_investigation(inc.id)  # background worker queue (Phase 4)
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
    logs (configured provider, via get_log_provider), Grafana links, Jira
    and Slack configuration. No tokens or secrets are ever included in
    this response."""
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
    # Phase 6 — which metrics provider the agents actually read: the
    # embedded telemetry store when no external Prometheus URL is set
    # ("embedded"), else the external Prometheus ("prometheus"). The
    # configured/reachable fields keep their original meaning (an
    # external Prometheus URL is set / reachable).
    try:
        prom_provider = getattr(get_prometheus_client(), "source", "prometheus") or "prometheus"
    except Exception:
        prom_provider = "prometheus" if prom_configured else "embedded"
    try:
        logs_provider = get_log_provider().provider_name
    except Exception:
        logs_provider = "none"
    return {
        "prometheus": {"configured": prom_configured, "reachable": prom_reachable, "url": prom_url,
                       "provider": prom_provider},
        "logs": {"provider": logs_provider, "configured": logs_provider != "none"},
        "grafana": grafana_status(),
        "jira": jira_status(),
        "slack": slack_status(),
    }


# Background investigations now run through the in-process worker queue
# (app/worker.py); /api/simulate below stays synchronous on purpose —
# its contract returns the investigation inline. See worker.py docstring.


@app.post("/api/simulate")
async def simulate(body: SimulateIn, request: Request, db: Session = Depends(get_db),
                   _limited: None = Depends(_rate_limit_investigate)):
    sc = get_scenario(body.scenario)
    inc = Incident(title=sc["title"], service=sc["service"], severity=sc["severity"], source="simulator",
                   external_id=f"sim-{uuid.uuid4().hex[:8]}", scenario=body.scenario)
    db.add(inc); db.commit(); db.refresh(inc)
    SERVICE_STATE.setdefault(sc["service"], {})["error_rate"] = sc["metrics"]["error_rate"]
    _event(db, inc.id, "alert", "simulator", {"title": sc["title"], "scenario": body.scenario})
    # Phase 6 — emit the simulated service's telemetry BEFORE the
    # investigation runs, tagged with this incident's external_id (the
    # run_id the agents scope their reads to). Only the scenario's logs
    # and metrics are emitted — never its root cause / remediation.
    emit_scenario_telemetry(sc, inc.external_id)
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


@app.post("/api/incidents/{incident_id}/notify")
async def notify(incident_id: str, body: NotifyIn, db: Session = Depends(get_db),
                 user: dict = Depends(_require_role({"admin", "responder"}))):
    """Phase 3 — notify a channel about an incident (admin/responder only).

    {"channel": "jira"} creates a Jira issue from the incident + top
    hypothesis/evidence summary, links the issue key back to the incident
    (idempotent: a second call returns the existing key, no duplicate
    issue), and returns {"ok", "channel", "issue_key", "issue_url"}.
    {"channel": "slack"} posts a formatted message (severity, service,
    confidence, incident id) and returns {"ok", "channel", "delivered"}.

    404 when the incident does not exist; 503 when the channel is not
    configured; 502 when the channel is configured but the call fails
    (bad response, timeout, unreachable). An invalid channel is a 422
    from the NotifyIn schema.
    """
    inc = db.query(Incident).filter_by(id=incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    if body.channel == "jira":
        if inc.jira_issue_key:
            url = None
            if (settings.jira_url or "").strip():
                url = f"{settings.jira_url.rstrip('/')}/browse/{inc.jira_issue_key}"
            return {"ok": True, "channel": "jira", "issue_key": inc.jira_issue_key,
                    "issue_url": url, "already_linked": True}
        client = JiraClient()
        if not client.configured:
            raise HTTPException(status_code=503, detail="Jira is not configured (set JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY)")
        hyps = db.query(Hypothesis).filter_by(incident_id=inc.id).order_by(Hypothesis.rank).all()
        ev_count = db.query(Evidence).filter_by(incident_id=inc.id).count()
        try:
            result = await client.create_issue(
                inc,
                hypotheses=[{"statement": h.statement, "confidence": h.confidence} for h in hyps],
                evidence_summary=f"{ev_count} evidence item(s) linked" if ev_count else None)
        except JiraError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        inc.jira_issue_key = result["key"]
        db.commit()
        _event(db, inc.id, "notification", user["email"],
               {"channel": "jira", "issue_key": result["key"], "issue_url": result["url"]})
        return {"ok": True, "channel": "jira", "issue_key": result["key"],
                "issue_url": result["url"], "already_linked": False}
    client = SlackClient()
    if not client.configured:
        raise HTTPException(status_code=503, detail="Slack is not configured (set SLACK_WEBHOOK_URL)")
    top = db.query(Hypothesis).filter_by(incident_id=inc.id).order_by(Hypothesis.rank).first()
    try:
        result = await client.notify(
            "hypothesis_ready" if top else "new_incident", inc,
            hypothesis={"statement": top.statement, "confidence": top.confidence} if top else None)
    except SlackError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    _event(db, inc.id, "notification", user["email"],
           {"channel": "slack", "delivered": True})
    return {"ok": True, "channel": "slack", "delivered": result.get("delivered", True)}


@app.post("/api/incidents/{incident_id}/investigate")
async def investigate(incident_id: str, request: Request, db: Session = Depends(get_db),
                      _limited: None = Depends(_rate_limit_investigate)):
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
    if plan.expires_at and plan.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        # An expired plan must be re-proposed, not approved: execute
        # would refuse it anyway, so approving it would be incoherent.
        plan.status = "expired"
        db.commit()
        raise HTTPException(status_code=400, detail="Plan expired — execution refused, re-approval required")
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
    if plan.status not in ("pending", "draft"):
        # Rejecting an approved/executed plan would flip its status
        # while the approval/execution audit trail says otherwise.
        raise HTTPException(status_code=400, detail=f"Plan is {plan.status}, cannot reject")
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
    # Approval-token note: the token is enforced when supplied (a wrong
    # token is 403), but it is NOT mandatory — the frontend never sends
    # it, and the same admin/responder role both approves and executes,
    # so the token is not a second factor here. The binding checks are
    # the role gate above plus execute_plan() requiring status approved,
    # a present token, and an unexpired plan. Making the token mandatory
    # would need the frontend to carry it from the approve response.
    if body.approval_token and body.approval_token != plan.approval_token:
        raise HTTPException(status_code=403, detail="Invalid approval token")
    inc = db.query(Incident).filter_by(id=plan.incident_id).first()
    result = execute_plan(db, plan, inc, dry_run=body.dry_run, executed_by=user["email"])
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Execution refused"))
    return result


@app.post("/api/incidents/{incident_id}/verify")
async def verify(incident_id: str, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    inc = db.query(Incident).filter_by(id=incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    result = verify_recovery(db, inc)
    # Phase 3 — best-effort Slack "resolved" notification (no-op when
    # Slack is not configured; never blocks verification).
    if result.get("passed"):
        await notify_slack_best_effort("resolved", inc)
    return result


@app.get("/api/documents")
def list_docs(db: Session = Depends(get_db)):
    return {"documents": [{"id": d.id, "title": d.title, "kind": d.kind, "service": d.service} for d in db.query(Document).all()]}


@app.post("/api/documents")
def add_doc(body: DocumentIn, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    doc = Document(title=body.title, kind=body.kind, service=body.service, content=body.content, embedding=hash_embedding(body.content))
    db.add(doc); db.commit()
    # No incident to attach an IncidentEvent to, so the audit trail for
    # knowledge-base writes is the server log.
    logger.info("Knowledge document added by %s: %s (%s)", user["email"], body.title, body.kind)
    return {"id": doc.id, "title": doc.title}


@app.post("/api/knowledge/search")
def knowledge(body: KnowledgeSearchIn, db: Session = Depends(get_db)):
    return search_knowledge(KnowledgeInput(query=body.query, service=body.service))


@app.get("/api/evals/results")
def eval_results(db: Session = Depends(get_db)):
    runs = db.query(EvalRun).order_by(EvalRun.run_at.desc()).limit(10).all()
    return {"runs": [{"id": r.id, "dataset_version": r.dataset_version, "model": r.model, "metrics": r.metrics, "run_at": r.run_at.isoformat() if r.run_at else None} for r in runs]}


@app.post("/api/evals/run")
async def run_eval(request: Request, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin"})),
                   _limited: None = Depends(_rate_limit_investigate)):
    from evals.run_eval import run_all
    metrics = await run_all(db)
    return {"metrics": metrics}
