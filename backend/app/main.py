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
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .agents import build_graph, run_investigation
from .config import settings, validate_production_settings
from .crypto import mask_secret, validate_credentials_key
from .database import SessionLocal, get_db, init_db
from .executor import execute_plan, postmortem_draft, verify_recovery
from .integrations.grafana import dashboard_url, grafana_status
from .integrations.jira import (JiraClient, JiraError, create_jira_issue_for_org,
                               get_jira_client_for_org, jira_status)
from .integrations.logs import get_log_provider, get_log_provider_for_org
from .integrations.prometheus import PrometheusClient, get_prometheus_client, get_prometheus_client_for_org
from .integrations.slack import (SlackClient, SlackError, get_slack_client_for_org,
                                post_slack_for_org, slack_status)
from .org_integrations import (INTEGRATION_NAMES, delete_org_integration,
                              get_all_org_integrations,
                              get_org_integration_decrypted,
                              mark_integration_tested, org_integration_status,
                              upsert_org_integration)
from .llm import hash_embedding
from .telemetry import emit_scenario_telemetry, store as telemetry_store
from .models import AgentRun, Document, EvalRun, Evidence, Hypothesis, Incident, IncidentEvent, Organization, OrganizationMembership, RemediationPlan, User
from .rate_limit import RateLimiter
from .schemas import AlertIn, AlertmanagerPayload, DocumentIn, ExecuteIn, KnowledgeSearchIn, LoginIn, NotifyIn, OrgIntegrationIn, OrgMemberIn, PlanEditIn, RejectIn, SignupIn, SimulateIn, incident_out
from .security import create_token, get_current_org, hash_password, is_legacy_hash, redact_pii, verify_password, verify_token
from .seed import DEMO_ORG_SLUG, get_or_create_demo_org, seed
from .synthetic import SERVICE_STATE, get_scenario, list_scenarios
from .tools import KnowledgeInput, search_knowledge
from .worker import enqueue_investigation, notify_slack_best_effort, worker

logger = logging.getLogger(__name__)

# Fail loudly at startup if a live deployment still uses the demo JWT
# secret. Env-driven and import-time on purpose: production settings come
# from the environment, fixed before the app starts (see config.py).
validate_production_settings(settings)

# Same idea for the per-org credential encryption key: without a
# persistent CREDENTIALS_ENCRYPTION_KEY a production deploy silently
# loses every saved integration secret on restart (see app/crypto.py).
validate_credentials_key()


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


# --- Phase 7: multi-tenancy helpers -----------------------------------------
# Scoping rule: every incident / document / eval row belongs to one org
# (org_id). Rows with org_id NULL pre-date multi-tenancy and are treated
# as demo-org rows. Authenticated callers see only their own org;
# unauthenticated reads see only the demo org. A cross-org id is a 404,
# never a 403, so one org cannot probe whether another org's ids exist.

def _optional_user(authorization: str | None = Header(default=None)) -> dict | None:
    """Bearer token when present and valid, else None (never raises)."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return verify_token(authorization[7:])


def _demo_org_id(db: Session) -> str:
    return get_or_create_demo_org(db).id


def _user_org_id(db: Session, user: dict) -> str:
    """Resolve the org for an authenticated token: token org_id first,
    then the user's primary org / membership, then the demo org (legacy
    users created before Phase 7 have no org)."""
    org_id = user.get("org_id")
    if org_id:
        return org_id
    db_user = db.query(User).filter_by(id=user.get("sub")).first() if user.get("sub") else None
    if db_user is not None and db_user.org_id:
        return db_user.org_id
    if db_user is not None:
        membership = db.query(OrganizationMembership).filter_by(user_id=db_user.id).first()
        if membership is not None:
            return membership.org_id
    return _demo_org_id(db)


def _resolve_org_id(db: Session, user: dict | None) -> str:
    """Org for a request: the caller's org when authenticated, else demo."""
    return _user_org_id(db, user) if user else _demo_org_id(db)


def _is_demo_org(db: Session, org_id: str) -> bool:
    org = db.query(Organization).filter_by(id=org_id).first()
    return bool(org and org.slug == DEMO_ORG_SLUG)


def _org_filter(model, org_id: str, is_demo: bool):
    """SQLAlchemy filter restricting ``model`` rows to one org. Demo-org
    callers also see pre–Phase 7 NULL-org rows; other orgs see only
    their own exact org_id."""
    if is_demo:
        return or_(model.org_id == org_id, model.org_id.is_(None))
    return model.org_id == org_id


def _scoped_incident(db: Session, incident_id: str, org_id: str) -> Incident:
    is_demo = _is_demo_org(db, org_id)
    inc = db.query(Incident).filter(Incident.id == incident_id, _org_filter(Incident, org_id, is_demo)).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    return inc


def _scoped_plan(db: Session, plan_id: str, org_id: str) -> RemediationPlan:
    plan = db.query(RemediationPlan).filter_by(id=plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    # The plan belongs to its incident's org; a cross-org plan id is 404.
    _scoped_incident(db, plan.incident_id, org_id)
    return plan


def _org_out(org: Organization) -> dict:
    return {"id": org.id, "name": org.name, "slug": org.slug, "plan": org.plan or "free"}


def _user_out(user: User, role: str | None = None) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email,
            "role": role or user.role, "org_id": user.org_id}


def _effective_role(db: Session, user: dict, org_id: str) -> str:
    """A user's role inside an org: membership role when present, else
    the token/user role."""
    db_user = db.query(User).filter_by(id=user.get("sub")).first() if user.get("sub") else None
    if db_user is not None:
        membership = db.query(OrganizationMembership).filter_by(org_id=org_id, user_id=db_user.id).first()
        if membership is not None:
            return membership.role
        return db_user.role
    return user.get("role", "viewer")


def _require_org_admin(db: Session, user: dict) -> Organization:
    org = get_current_org(db, user)
    if _effective_role(db, user, org.id) != "admin":
        raise HTTPException(status_code=403, detail="Organization admin role required")
    return org


def _slugify_company(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-") or "org"
    return f"{slug}-{secrets.token_hex(3)}"


def _store_external_id(org_id: str, ext: str, is_demo: bool) -> str:
    """Namespace stored external_id per org (the column is globally
    UNIQUE, so per-org dedup needs distinct stored values). Demo keeps
    the plain value; other orgs store "<org_id>:<external_id>"."""
    return ext if is_demo else f"{org_id}:{ext}"


def _detail(db: Session, incident: Incident) -> dict:
    hyps = db.query(Hypothesis).filter_by(incident_id=incident.id).order_by(Hypothesis.rank).all()
    evs = db.query(Evidence).filter_by(incident_id=incident.id).all()
    events = db.query(IncidentEvent).filter_by(incident_id=incident.id).order_by(IncidentEvent.created_at).all()
    plans = db.query(RemediationPlan).filter_by(incident_id=incident.id).order_by(RemediationPlan.created_at.desc()).all()
    runs = db.query(AgentRun).filter_by(incident_id=incident.id).all()
    plan = plans[0] if plans else None
    # Monitoring links/config for this incident's service. Guarded so a
    # failing integration helper can never break the detail response.
    # Prometheus configured-ness honours the incident's workspace (org)
    # integration, not just the global env URL — a workspace whose test
    # passed must not render as "not connected" here. last_test_ok is
    # the workspace integration's last connectivity-test outcome (True /
    # False / None when never tested).
    try:
        _org_id = incident.org_id or get_or_create_demo_org(db).id
        _prom_entry = next((e for e in org_integration_status(db, _org_id)
                            if e.get("name") == "prometheus"), None)
        monitoring = {
            "grafana_dashboard_url": dashboard_url(incident.service),
            "prometheus_configured": bool((settings.prometheus_url or "").strip())
                or bool(_prom_entry and _prom_entry.get("configured")),
            "prometheus_last_test_ok": _prom_entry.get("last_test_ok") if _prom_entry else None,
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


@app.post("/api/auth/signup")
def signup(body: SignupIn, request: Request, db: Session = Depends(get_db),
           _limited: None = Depends(_rate_limit_login)):
    """Phase 7 — create a company (Organization) + its admin user.

    Rate-limited like login. Duplicate email is a 400 (the email is
    globally unique across orgs). Returns {token, user, organization};
    the token carries org_id. The org slug is derived from the company
    name plus a short random suffix so two companies with the same name
    never collide."""
    email = (body.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=422, detail="Invalid email address")
    if db.query(User).filter_by(email=email).first():
        raise HTTPException(status_code=400, detail="An account with this email already exists")
    org = Organization(name=body.company_name.strip(), slug=_slugify_company(body.company_name), plan="free")
    db.add(org)
    db.commit()
    db.refresh(org)
    user = User(name=body.admin_name.strip(), email=email, password_hash=hash_password(body.password),
                role="admin", org_id=org.id)
    db.add(user)
    db.commit()
    db.refresh(user)
    db.add(OrganizationMembership(org_id=org.id, user_id=user.id, role="admin"))
    db.commit()
    logger.info("Signup: org %s created by %s from %s", org.slug, email, _client_ip(request))
    return {"token": create_token(user.id, user.email, user.role, org_id=org.id),
            "user": _user_out(user, role="admin"), "organization": _org_out(org)}


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
    # Phase 7 — resolve the user's org (primary org, else membership,
    # else demo) and backfill it so the token always carries org_id.
    # Legacy / directly-created users with no org become demo-org users.
    org_id = user.org_id
    if not org_id:
        membership = db.query(OrganizationMembership).filter_by(user_id=user.id).first()
        org_id = membership.org_id if membership else _demo_org_id(db)
        user.org_id = org_id
        if not membership:
            db.add(OrganizationMembership(org_id=org_id, user_id=user.id, role=user.role))
        db.commit()
    org = db.query(Organization).filter_by(id=org_id).first()
    logger.info("Login succeeded for %s (%s) from %s", user.email, user.role, _client_ip(request))
    return {"token": create_token(user.id, user.email, user.role, org_id=org_id),
            "user": _user_out(user), "organization": _org_out(org) if org else None}


@app.get("/api/orgs/me")
def org_me(db: Session = Depends(get_db), user: dict = Depends(_require_user)):
    """Phase 7 — current org + the caller's memberships and role. The
    org webhook token is shown masked, and only to org admins."""
    org = get_current_org(db, user)
    role = _effective_role(db, user, org.id)
    db_user = db.query(User).filter_by(id=user.get("sub")).first()
    memberships = []
    if db_user is not None:
        for m in db.query(OrganizationMembership).filter_by(user_id=db_user.id).all():
            m_org = db.query(Organization).filter_by(id=m.org_id).first()
            memberships.append({"org_id": m.org_id, "org_name": m_org.name if m_org else None,
                                "org_slug": m_org.slug if m_org else None, "role": m.role})
    return {"organization": {**_org_out(org),
                             "webhook_token_masked": mask_secret(org.webhook_token) if role == "admin" else None},
            "role": role, "memberships": memberships}


@app.get("/api/orgs/members")
def list_org_members(db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    """Phase 7 — members of the caller's org (admin/responder)."""
    org = get_current_org(db, user)
    out = []
    for m in db.query(OrganizationMembership).filter_by(org_id=org.id).all():
        member = db.query(User).filter_by(id=m.user_id).first()
        if member is not None:
            out.append(_user_out(member, role=m.role))
    return {"organization": _org_out(org), "members": out}


@app.post("/api/orgs/members")
def add_org_member(body: OrgMemberIn, db: Session = Depends(get_db), user: dict = Depends(_require_user)):
    """Phase 7 — org admin only. Adds an existing user (by email) to the
    caller's org with the given role, or creates a new user in the org.
    Returns the masked user (never a password). NOTE: a newly created
    user gets a random unusable password — there is no password-set /
    reset flow yet, so a new invitee cannot log in until one exists;
    adding an existing user works end to end today."""
    org = _require_org_admin(db, user)
    email = (body.email or "").strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="Invalid email address")
    existing = db.query(User).filter_by(email=email).first()
    if existing is None:
        # Random unusable password (not returned, not a real credential):
        # the invitee needs a password-set flow (out of scope for Phase 7).
        new_user = User(name=email.split("@")[0], email=email,
                        password_hash=hash_password(secrets.token_urlsafe(24)),
                        role=body.role, org_id=org.id)
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        db.add(OrganizationMembership(org_id=org.id, user_id=new_user.id, role=body.role))
        db.commit()
        return {"created": True, "user": _user_out(new_user, role=body.role), "organization": _org_out(org)}
    membership = db.query(OrganizationMembership).filter_by(org_id=org.id, user_id=existing.id).first()
    if membership is None:
        db.add(OrganizationMembership(org_id=org.id, user_id=existing.id, role=body.role))
        if not existing.org_id:
            existing.org_id = org.id
        db.commit()
    else:
        membership.role = body.role
        db.commit()
    return {"created": False, "user": _user_out(existing, role=body.role), "organization": _org_out(org)}


@app.post("/api/orgs/webhook-token/rotate")
def rotate_webhook_token(db: Session = Depends(get_db), user: dict = Depends(_require_user)):
    """Phase 7 — org admin only. Rotates the org's inbound webhook
    token. The new full token is returned ONCE here and never again
    (GET /api/orgs/me shows only the masked form afterwards)."""
    org = _require_org_admin(db, user)
    org.webhook_token = secrets.token_urlsafe(32)
    db.commit()
    db.refresh(org)
    return {"webhook_token": org.webhook_token, "masked": mask_secret(org.webhook_token),
            "organization": _org_out(org)}


@app.post("/api/webhooks/alert")
async def webhook(request: Request, db: Session = Depends(get_db),
                  _limited: None = Depends(_rate_limit_webhook),
                  user: dict | None = Depends(_optional_user)):
    """Accept both shapes (Phase 3):
    - the original AlertIn JSON {title, service, severity, source, external_id}
      — behaviour unchanged from v1;
    - a Prometheus Alertmanager payload, detected by the "alerts" key.

    Optional shared-secret auth (Phase 8): when ALERT_WEBHOOK_SECRET is
    set, callers must send it as header ``X-Alert-Token`` or the webhook
    returns 401. When unset (the demo default) the webhook stays open so
    Alertmanager needs no extra configuration.

    Phase 7 — scoping: with a valid bearer token the alert lands in the
    caller's org; without one it lands in the demo org. Dedup is per-org
    (see _store_external_id).
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
    org_id = _resolve_org_id(db, user)
    return await _handle_alert_payload(payload, db, org_id)


@app.post("/api/webhooks/alert/{org_token}")
async def webhook_for_org(org_token: str, request: Request, db: Session = Depends(get_db),
                          _limited: None = Depends(_rate_limit_webhook)):
    """Phase 7 — per-org inbound webhook. The org is resolved by its
    webhook token (rotated via POST /api/orgs/webhook-token/rotate);
    an unknown token is a 404 so it reveals nothing. Handling is
    identical to the legacy webhook, scoped to that org with per-org
    dedup. The shared ALERT_WEBHOOK_SECRET does not apply here — the
    per-org token is itself the credential."""
    org = db.query(Organization).filter_by(webhook_token=org_token).first()
    if not org:
        raise HTTPException(status_code=404, detail="Unknown webhook token")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid alert payload: body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid alert payload: body must be a JSON object")
    return await _handle_alert_payload(payload, db, org.id)


async def _handle_alert_payload(payload: dict, db: Session, org_id: str) -> dict:
    """Shared alert handling for the legacy and per-org webhooks."""
    if "alerts" in payload:
        return await _webhook_alertmanager(payload, db, org_id)
    try:
        body = AlertIn.model_validate(payload)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid alert payload: title and service are required")
    is_demo = _is_demo_org(db, org_id)
    ext = body.external_id or f"alert-{uuid.uuid4().hex[:8]}"
    stored_ext = _store_external_id(org_id, ext, is_demo)
    existing = db.query(Incident).filter_by(external_id=stored_ext).first()
    if existing:
        return {"incident_id": existing.id, "deduplicated": True, **incident_out(existing)}
    inc = Incident(title=body.title, service=body.service, severity=body.severity, source=body.source,
                   external_id=stored_ext, org_id=org_id)
    db.add(inc); db.commit(); db.refresh(inc)
    _event(db, inc.id, "alert", body.source, {"title": redact_pii(body.title), "service": body.service, "severity": body.severity})
    enqueue_investigation(inc.id, org_id=org_id)  # background worker queue (Phase 4)
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


async def _webhook_alertmanager(payload: dict, db: Session, org_id: str) -> dict:
    try:
        body = AlertmanagerPayload.model_validate(payload)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid Alertmanager payload: alerts must be a list of alert objects")
    if not body.alerts:
        raise HTTPException(status_code=422, detail="Invalid Alertmanager payload: alerts list is empty")
    is_demo = _is_demo_org(db, org_id)
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
        ext = _store_external_id(org_id, _am_external_id(alert), is_demo)
        existing = db.query(Incident).filter_by(external_id=ext).first()
        if existing and existing.status != "resolved":
            dedup_ids.append(existing.id)
            continue
        if existing and existing.status == "resolved":
            # external_id is unique: a re-firing alert for a resolved incident
            # gets a fresh id so the new incident can be created.
            ext = f"{ext}-{uuid.uuid4().hex[:8]}"
        inc = Incident(title=title, service=service, severity=severity,
                       source="alertmanager", external_id=ext, scenario="", org_id=org_id)
        db.add(inc); db.commit(); db.refresh(inc)
        # Alert annotations are untrusted external text — redact PII /
        # secrets before they land in the audit trail (the incident
        # title itself is kept as-is for display; the event payload is
        # the copy that gets redacted).
        _event(db, inc.id, "alert", "alertmanager",
               {"title": redact_pii(title), "service": service, "severity": severity,
                "alertname": labels.get("alertname", ""), "description": redact_pii(description),
                "fingerprint": alert.fingerprint or ""})
        enqueue_investigation(inc.id, org_id=org_id)  # background worker queue (Phase 4)
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
async def integrations_status(db: Session = Depends(get_db),
                              user: dict | None = Depends(_optional_user)):
    """Phase 2/3/4 status: Prometheus (live health check when configured),
    logs (configured provider, via get_log_provider), Grafana links, Jira
    and Slack configuration. No tokens or secrets are ever included in
    this response.

    Phase 7: ``org_integrations`` adds one entry per known integration
    for the caller's org (demo org when unauthenticated — this route
    stays open so the existing unauthenticated callers, the frontend
    status panel and the pre–Phase 7 tests keep working): each entry is
    ``{name, configured, provider, masked_url, last_test_ok, source}``
    where ``source`` is "org" (a per-org credential exists), "global"
    (only env config exists) or "none". No secret values, masked or
    otherwise, appear anywhere in this response.
    """
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
    org_id = _resolve_org_id(db, user)
    try:
        org_entries = org_integration_status(db, org_id)
    except Exception:
        org_entries = []
    for entry in org_entries:
        entry["source"] = ("org" if entry.get("configured")
                           else ("global" if _global_configured(entry["name"]) else "none"))
    return {
        "prometheus": {"configured": prom_configured, "reachable": prom_reachable, "url": prom_url,
                       "provider": prom_provider},
        "logs": {"provider": logs_provider, "configured": logs_provider != "none"},
        "grafana": grafana_status(),
        "jira": jira_status(),
        "slack": slack_status(),
        "org_integrations": org_entries,
    }


def _global_configured(name: str) -> bool:
    """Whether an integration is configured via global env settings
    (Phase 7 status ``source`` field). No secrets are read."""
    if name == "prometheus":
        return bool((settings.prometheus_url or "").strip())
    if name == "logs":
        return bool((settings.elasticsearch_url or "").strip()
                    or (settings.opensearch_url or "").strip())
    if name == "grafana":
        return bool((settings.grafana_url or "").strip())
    if name == "jira":
        return bool((settings.jira_url or "").strip()
                    and (settings.jira_email or "").strip()
                    and (settings.jira_api_token or "").strip()
                    and (settings.jira_project_key or "").strip())
    if name == "slack":
        return bool((settings.slack_webhook_url or "").strip())
    return False


@app.get("/api/orgs/integrations")
def list_org_integrations(db: Session = Depends(get_db),
                          user: dict = Depends(_require_role({"admin", "responder"}))):
    """Phase 7 — masked list of the caller's org integrations
    (admin/responder). Secret values appear only masked, never plaintext."""
    org = get_current_org(db, user)
    return {"organization": _org_out(org),
            "integrations": get_all_org_integrations(db, org.id)}


@app.put("/api/orgs/integrations/{name}")
def put_org_integration(name: str, body: OrgIntegrationIn, db: Session = Depends(get_db),
                        user: dict = Depends(_require_user)):
    """Phase 7 — org admin only. Upsert one per-org integration
    (``config`` = non-secret fields, ``secrets`` = encrypted on storage).
    Returns the masked view. An unknown ``name`` is a 400; a body whose
    ``name`` does not match the path is also a 400."""
    org = _require_org_admin(db, user)
    if name not in INTEGRATION_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown integration {name!r}; expected one of {sorted(INTEGRATION_NAMES)}")
    if body.name != name:
        raise HTTPException(status_code=400, detail="Body 'name' must match the path integration name")
    try:
        return upsert_org_integration(db, org.id, name, provider=body.provider,
                                      config=dict(body.config or {}),
                                      secrets=dict(body.secrets or {}))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/orgs/integrations/{name}")
def delete_org_integration_route(name: str, db: Session = Depends(get_db),
                                 user: dict = Depends(_require_user)):
    """Phase 7 — org admin only. Delete one per-org integration
    (config + encrypted secrets). 404 when it was not configured."""
    org = _require_org_admin(db, user)
    if name not in INTEGRATION_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown integration {name!r}; expected one of {sorted(INTEGRATION_NAMES)}")
    try:
        deleted = delete_org_integration(db, org.id, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Integration {name!r} is not configured for this organization")
    return {"ok": True, "name": name, "deleted": True}


@app.post("/api/integrations/{name}/test")
async def test_integration(name: str, request: Request, db: Session = Depends(get_db),
                           _limited: None = Depends(_rate_limit_investigate),
                           user: dict = Depends(_require_role({"admin", "responder"}))):
    """Phase 7 — test one integration for the caller's org (admin/responder).

    Performs a real lightweight health check against the org's configured
    integration (falling back to the global env config when the org has
    none): Prometheus ``health()``; logs provider ``health()``; Grafana
    GET on the dashboard base URL; Jira GET /rest/api/3/myself with Basic
    auth; Slack — webhook URL shape validation plus a GET/HEAD reachability
    check, NEVER posting a test message to a real channel. Rate-limited
    like the other LLM-cost endpoints (shared investigate limiter).
    Returns ``{name, ok, message}`` with no secrets; the outcome is
    recorded via ``mark_integration_tested`` when the org has the
    integration configured. Unknown ``name`` is a 400.
    """
    org_id = _user_org_id(db, user)
    if name not in INTEGRATION_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown integration {name!r}; expected one of {sorted(INTEGRATION_NAMES)}")
    ok = False
    message = ""
    try:
        if name == "prometheus":
            if get_org_integration_decrypted(db, org_id, "prometheus") is None \
                    and not (settings.prometheus_url or "").strip():
                message = "prometheus is not configured for this organization"
            else:
                client = get_prometheus_client_for_org(db, org_id)
                health = await client.health() if client is not None else {"reachable": False, "error": "prometheus is not configured"}
                ok = bool(health.get("reachable"))
                message = "Prometheus reachable" if ok else (health.get("error") or "Prometheus unreachable")
        elif name == "logs":
            if get_org_integration_decrypted(db, org_id, "logs") is None and not _global_configured("logs"):
                message = "no log provider configured for this organization"
            else:
                provider = get_log_provider_for_org(db, org_id)
                health = await provider.health()
                ok = bool(health.get("reachable"))
                message = f"{provider.provider_name} reachable" if ok else (health.get("error") or "log provider unreachable")
        elif name == "grafana":
            base = ""
            try:
                integ = get_org_integration_decrypted(db, org_id, "grafana")
                base = ((integ or {}).get("config") or {}).get("url") or ""
            except Exception:
                base = ""
            base = (base or settings.grafana_url or "").strip()
            if not base:
                message = "grafana is not configured for this organization"
            else:
                try:
                    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as http_client:
                        resp = await http_client.get(base.rstrip("/"))
                    ok = 200 <= resp.status_code < 500
                    message = "Grafana reachable" if ok else f"Grafana returned HTTP {resp.status_code}"
                except httpx.HTTPError:
                    message = "Grafana connection failed"
        elif name == "jira":
            client = get_jira_client_for_org(db, org_id)
            if client is None:
                message = "jira is not configured for this organization"
            else:
                try:
                    await client._request("GET", "/rest/api/3/myself")
                    ok = True
                    message = "Jira reachable"
                except JiraError as exc:
                    message = str(exc)
        elif name == "slack":
            webhook_url = ""
            try:
                integ = get_org_integration_decrypted(db, org_id, "slack")
                webhook_url = ((integ or {}).get("secrets") or {}).get("webhook_url") or ""
            except Exception:
                webhook_url = ""
            webhook_url = (webhook_url or settings.slack_webhook_url or "").strip()
            if not webhook_url:
                message = "slack is not configured for this organization"
            else:
                from urllib.parse import urlparse
                parsed = urlparse(webhook_url)
                if parsed.scheme != "https" or not parsed.netloc or not parsed.path.strip("/"):
                    message = "Slack webhook URL is not a valid https URL"
                else:
                    # Reachability check only — a GET, never a POST, so
                    # no test message is ever delivered to the channel.
                    try:
                        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as http_client:
                            await http_client.get(webhook_url)
                        ok = True
                        message = "Slack webhook URL reachable (no message posted)"
                    except httpx.HTTPError:
                        message = "Slack webhook URL unreachable"
    except Exception as exc:  # never leak internals or secrets in the message
        logger.warning("Integration test for %s failed: %s", name, type(exc).__name__)
        ok = False
        message = message or "integration test failed"
    try:
        mark_integration_tested(db, org_id, name, ok)
    except Exception:
        pass
    return {"name": name, "ok": ok, "message": message}


# Background investigations now run through the in-process worker queue
# (app/worker.py); /api/simulate below stays synchronous on purpose —
# its contract returns the investigation inline. See worker.py docstring.


@app.post("/api/simulate")
async def simulate(body: SimulateIn, request: Request, db: Session = Depends(get_db),
                   _limited: None = Depends(_rate_limit_investigate),
                   user: dict | None = Depends(_optional_user)):
    org_id = _resolve_org_id(db, user)
    sc = get_scenario(body.scenario)
    inc = Incident(title=sc["title"], service=sc["service"], severity=sc["severity"], source="simulator",
                   external_id=f"sim-{uuid.uuid4().hex[:8]}", scenario=body.scenario, org_id=org_id)
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
def list_incidents(status: str | None = None, service: str | None = None, db: Session = Depends(get_db),
                   user: dict | None = Depends(_optional_user)):
    org_id = _resolve_org_id(db, user)
    q = db.query(Incident).filter(_org_filter(Incident, org_id, _is_demo_org(db, org_id))).order_by(Incident.opened_at.desc())
    if status:
        q = q.filter_by(status=status)
    if service:
        q = q.filter_by(service=service)
    return {"incidents": [incident_out(i) for i in q.all()], "demo_mode": settings.demo_mode}


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str, db: Session = Depends(get_db),
                 user: dict | None = Depends(_optional_user)):
    inc = _scoped_incident(db, incident_id, _resolve_org_id(db, user))
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
    inc = _scoped_incident(db, incident_id, _user_org_id(db, user))
    # Phase 7 — notify via the incident's org credentials (falling back
    # to the global env config when the org has none). A NULL org_id
    # (pre–Phase 7 row) resolves to the demo org.
    org_id = inc.org_id or _demo_org_id(db)
    if body.channel == "jira":
        if inc.jira_issue_key:
            base = (settings.jira_url or "").strip()
            try:
                org_jira = get_org_integration_decrypted(db, org_id, "jira")
                if org_jira:
                    base = ((org_jira.get("config") or {}).get("url") or "").strip() or base
            except Exception:
                pass
            url = f"{base.rstrip('/')}/browse/{inc.jira_issue_key}" if base else None
            return {"ok": True, "channel": "jira", "issue_key": inc.jira_issue_key,
                    "issue_url": url, "already_linked": True}
        hyps = db.query(Hypothesis).filter_by(incident_id=inc.id).order_by(Hypothesis.rank).all()
        ev_count = db.query(Evidence).filter_by(incident_id=inc.id).count()
        try:
            result = await create_jira_issue_for_org(
                db, org_id, inc,
                hypotheses=[{"statement": h.statement, "confidence": h.confidence} for h in hyps],
                evidence_summary=f"{ev_count} evidence item(s) linked" if ev_count else None)
        except JiraError as exc:
            if "not configured" in str(exc):
                raise HTTPException(status_code=503, detail="Jira is not configured (set JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY)")
            raise HTTPException(status_code=502, detail=str(exc))
        inc.jira_issue_key = result["key"]
        db.commit()
        _event(db, inc.id, "notification", user["email"],
               {"channel": "jira", "issue_key": result["key"], "issue_url": result["url"]})
        return {"ok": True, "channel": "jira", "issue_key": result["key"],
                "issue_url": result["url"], "already_linked": False}
    top = db.query(Hypothesis).filter_by(incident_id=inc.id).order_by(Hypothesis.rank).first()
    try:
        result = await post_slack_for_org(
            db, org_id, "hypothesis_ready" if top else "new_incident", inc,
            hypothesis={"statement": top.statement, "confidence": top.confidence} if top else None)
    except SlackError as exc:
        if "not configured" in str(exc):
            raise HTTPException(status_code=503, detail="Slack is not configured (set SLACK_WEBHOOK_URL)")
        raise HTTPException(status_code=502, detail=str(exc))
    _event(db, inc.id, "notification", user["email"],
           {"channel": "slack", "delivered": True})
    return {"ok": True, "channel": "slack", "delivered": result.get("delivered", True)}


@app.post("/api/incidents/{incident_id}/investigate")
async def investigate(incident_id: str, request: Request, db: Session = Depends(get_db),
                      _limited: None = Depends(_rate_limit_investigate),
                      user: dict | None = Depends(_optional_user)):
    inc = _scoped_incident(db, incident_id, _resolve_org_id(db, user))
    return await run_investigation(db, inc)


@app.get("/api/incidents/{incident_id}/stream")
async def stream(incident_id: str, db: Session = Depends(get_db),
                 user: dict | None = Depends(_optional_user)):
    inc = _scoped_incident(db, incident_id, _resolve_org_id(db, user))
    events = db.query(IncidentEvent).filter_by(incident_id=incident_id).order_by(IncidentEvent.created_at).all()

    async def gen():
        for e in events:
            yield f"data: {json.dumps({'type': e.type, 'actor': e.actor, 'payload': e.payload})}\n\n"
            await asyncio.sleep(0.05)
        yield f"data: {json.dumps({'type': 'done', 'status': inc.status})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/plans/{plan_id}/approve")
def approve(plan_id: str, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    plan = _scoped_plan(db, plan_id, _user_org_id(db, user))
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
    plan = _scoped_plan(db, plan_id, _user_org_id(db, user))
    if plan.status not in ("pending", "draft"):
        # Rejecting an approved/executed plan would flip its status
        # while the approval/execution audit trail says otherwise.
        raise HTTPException(status_code=400, detail=f"Plan is {plan.status}, cannot reject")
    plan.status = "rejected"; db.commit()
    _event(db, plan.incident_id, "approval", user["email"], {"message": f"Plan rejected: {body.reason}", "plan_id": plan.id})
    return {"ok": True, "plan_id": plan.id, "status": "rejected"}


@app.post("/api/plans/{plan_id}/edit")
def edit_plan(plan_id: str, body: PlanEditIn, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    plan = _scoped_plan(db, plan_id, _user_org_id(db, user))
    if plan.status not in ("pending", "draft"):
        raise HTTPException(status_code=400, detail=f"Plan is {plan.status}, cannot edit")
    plan.steps = body.steps; db.commit()
    _event(db, plan.incident_id, "approval", user["email"], {"message": "Plan edited before approval", "plan_id": plan.id})
    return {"ok": True, "plan_id": plan.id, "steps": plan.steps}


@app.post("/api/plans/{plan_id}/execute")
def execute(plan_id: str, body: ExecuteIn, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    plan = _scoped_plan(db, plan_id, _user_org_id(db, user))
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
    inc = _scoped_incident(db, incident_id, _user_org_id(db, user))
    result = verify_recovery(db, inc)
    # Phase 3 — best-effort Slack "resolved" notification (no-op when
    # Slack is not configured; never blocks verification).
    if result.get("passed"):
        await notify_slack_best_effort("resolved", inc)
    return result


@app.get("/api/documents")
def list_docs(db: Session = Depends(get_db), user: dict | None = Depends(_optional_user)):
    org_id = _resolve_org_id(db, user)
    docs = db.query(Document).filter(_org_filter(Document, org_id, _is_demo_org(db, org_id))).all()
    return {"documents": [{"id": d.id, "title": d.title, "kind": d.kind, "service": d.service} for d in docs]}


@app.post("/api/documents")
def add_doc(body: DocumentIn, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin", "responder"}))):
    doc = Document(title=body.title, kind=body.kind, service=body.service, content=body.content,
                   embedding=hash_embedding(body.content), org_id=_user_org_id(db, user))
    db.add(doc); db.commit()
    # No incident to attach an IncidentEvent to, so the audit trail for
    # knowledge-base writes is the server log.
    logger.info("Knowledge document added by %s: %s (%s)", user["email"], body.title, body.kind)
    return {"id": doc.id, "title": doc.title}


@app.post("/api/knowledge/search")
def knowledge(body: KnowledgeSearchIn, db: Session = Depends(get_db),
              user: dict | None = Depends(_optional_user)):
    # Phase 7 note: search_knowledge answers from the shared static
    # runbook/postmortem fixtures (RUNBOOKS + POSTMORTEMS in synthetic.py),
    # not from per-org Document rows, so there is no per-org content to
    # leak here. Per-org Document rows are scoped in /api/documents above;
    # wiring org-scoped DB documents into this search is Worker B / later
    # work (the search tool would need an org-aware corpus).
    return search_knowledge(KnowledgeInput(query=body.query, service=body.service))


@app.get("/api/evals/results")
def eval_results(db: Session = Depends(get_db), user: dict | None = Depends(_optional_user)):
    org_id = _resolve_org_id(db, user)
    runs = db.query(EvalRun).filter(_org_filter(EvalRun, org_id, _is_demo_org(db, org_id))).order_by(EvalRun.run_at.desc()).limit(10).all()
    return {"runs": [{"id": r.id, "dataset_version": r.dataset_version, "model": r.model, "metrics": r.metrics, "run_at": r.run_at.isoformat() if r.run_at else None} for r in runs]}


@app.post("/api/evals/run")
async def run_eval(request: Request, db: Session = Depends(get_db), user: dict = Depends(_require_role({"admin"})),
                   _limited: None = Depends(_rate_limit_investigate)):
    from evals.run_eval import run_all
    org_id = _user_org_id(db, user)
    # run_all() creates its EvalRun row + eval incidents without an org;
    # tag everything it created with the caller's org afterwards.
    eval_ids_before = {r.id for r in db.query(EvalRun).all()}
    inc_ids_before = {i.id for i in db.query(Incident).all()}
    metrics = await run_all(db)
    db.query(EvalRun).filter(EvalRun.id.notin_(eval_ids_before)).update({EvalRun.org_id: org_id})
    db.query(Incident).filter(Incident.id.notin_(inc_ids_before)).update({Incident.org_id: org_id})
    db.commit()
    return {"metrics": metrics}
