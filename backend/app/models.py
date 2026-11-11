"""Data model — PRD §9. UUID PKs, created_at/updated_at on every table."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint

from .database import Base


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


def _webhook_token():
    import secrets
    return secrets.token_urlsafe(32)


class Organization(Base):
    """Phase 7 — a tenant (company). One org per signup; demo users live
    in the seeded org with slug "demo"."""
    __tablename__ = "organizations"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    plan = Column(String, default="free")  # free | pro | ...
    # Per-org inbound webhook token (secret; only ever returned in full
    # at creation / rotation, otherwise masked). Unique so a token
    # unambiguously resolves to one org.
    webhook_token = Column(String, unique=True, nullable=False, index=True, default=_webhook_token)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class OrganizationMembership(Base):
    """Phase 7 — team membership. This table is the authority for which
    users belong to which org; ``User.org_id`` is the user's primary org
    (a convenience, kept in sync on signup / invite)."""
    __tablename__ = "organization_memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_membership_org_user"),)
    id = Column(String, primary_key=True, default=_uuid)
    org_id = Column(String, ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String, default="viewer")  # admin | responder | viewer
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class OrganizationIntegration(Base):
    """Phase 7 — one org's config for one integration (prometheus, logs,
    grafana, jira, slack). ``config_json`` holds NON-secret fields only
    (url, project, index, channel); ``secret_json`` holds ENCRYPTED
    secret fields only (token / api_key / webhook_url / password) — see
    app/org_integrations.py, which is the only writer. Never plaintext."""
    __tablename__ = "organization_integrations"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_org_integration_org_name"),)
    id = Column(String, primary_key=True, default=_uuid)
    org_id = Column(String, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String, nullable=False, index=True)  # prometheus | logs | grafana | jira | slack
    provider = Column(String, default="")  # e.g. elasticsearch | opensearch for logs
    config_json = Column(Text, default="{}")
    secret_json = Column(Text, default="{}")
    is_active = Column(Boolean, default=True)
    last_tested_at = Column(DateTime, nullable=True)
    last_test_ok = Column(Boolean, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="viewer")  # admin | responder | viewer
    # Phase 7 — primary org (nullable for migration safety; the
    # membership table is the authority). NULL rows are treated as the
    # demo org by the API scoping helpers.
    org_id = Column(String, ForeignKey("organizations.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class Incident(Base):
    __tablename__ = "incidents"
    id = Column(String, primary_key=True, default=_uuid)
    title = Column(String, nullable=False)
    service = Column(String, nullable=False, index=True)
    severity = Column(String, default="P2")  # P1 | P2 | P3
    status = Column(String, default="open", index=True)
    # open | investigating | awaiting_approval | approved | executing | verifying | resolved | abstained
    source = Column(String, default="simulator")
    external_id = Column(String, unique=True, index=True)
    scenario = Column(String, default="")  # synthetic scenario key, "" = ad-hoc
    # Phase 7 — owning org (nullable for migration safety; NULL rows are
    # treated as the demo org by the API scoping helpers). Note the
    # external_id UNIQUE constraint is global: per-org dedup is achieved
    # by namespacing the stored value as "<org_id>:<external_id>" for
    # non-demo orgs (see app/main.py _store_external_id); incident_out
    # never exposes external_id, so callers never see the prefix.
    org_id = Column(String, ForeignKey("organizations.id"), nullable=True, index=True)
    # Phase 3 — Jira issue created for this incident, if any (nullable; an
    # existing DB gains the column via add_missing_columns in database.py).
    jira_issue_key = Column(String, nullable=True)
    opened_at = Column(DateTime, default=_now)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    id = Column(String, primary_key=True, default=_uuid)
    incident_id = Column(String, ForeignKey("incidents.id"), index=True, nullable=False)
    type = Column(String, nullable=False)  # alert | agent | evidence | hypothesis | approval | execution | verification | postmortem
    actor = Column(String, default="system")
    payload = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id = Column(String, primary_key=True, default=_uuid)
    incident_id = Column(String, ForeignKey("incidents.id"), index=True, nullable=False)
    agent = Column(String, nullable=False)
    prompt_version = Column(String, default="v1")
    model = Column(String, default="demo")
    # NULL = the agent ran no LLM call (deterministic specialist agents).
    # 0 = an LLM call genuinely used 0 tokens (demo-mode supervisor).
    # No default: SQLAlchemy would apply a default=0 over an explicit
    # None on INSERT, which would erase the NULL-vs-0 distinction.
    # The column was already nullable, so this is a Python-side change
    # only — no DB migration.
    tokens = Column(Integer, nullable=True)
    cost_usd = Column(Float, default=0.0)
    latency_ms = Column(Integer, default=0)
    status = Column(String, default="done")
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class Evidence(Base):
    __tablename__ = "evidence"
    id = Column(String, primary_key=True, default=_uuid)
    incident_id = Column(String, ForeignKey("incidents.id"), index=True, nullable=False)
    kind = Column(String, nullable=False)  # log | metric | doc
    source_ref = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    captured_at = Column(DateTime, default=_now)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class Hypothesis(Base):
    __tablename__ = "hypotheses"
    id = Column(String, primary_key=True, default=_uuid)
    incident_id = Column(String, ForeignKey("incidents.id"), index=True, nullable=False)
    rank = Column(Integer, default=1)
    statement = Column(Text, nullable=False)
    confidence = Column(Float, default=0.0)
    status = Column(String, default="proposed")
    evidence_ids = Column(JSON, default=list)
    # Phase 6 — hypothesis record enrichment (JSON lists, default empty).
    related_previous_incidents = Column(JSON, default=list)  # [{title, kind}] postmortems from Knowledge agent
    alternatives = Column(JSON, default=list)  # alternative explanation texts, or a note when none is supported
    next_steps = Column(JSON, default=list)  # recommended next steps (remediation plan / runbook descriptions)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class RemediationPlan(Base):
    __tablename__ = "remediation_plans"
    id = Column(String, primary_key=True, default=_uuid)
    incident_id = Column(String, ForeignKey("incidents.id"), index=True, nullable=False)
    steps = Column(JSON, default=list)  # [{tool, args, description, risk}]
    risk = Column(String, default="medium")
    rollback = Column(Text, default="")
    status = Column(String, default="draft", index=True)  # draft|pending|approved|rejected|expired|executed
    approved_by = Column(String, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    approval_token = Column(String, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ActionExecution(Base):
    __tablename__ = "action_executions"
    id = Column(String, primary_key=True, default=_uuid)
    plan_id = Column(String, ForeignKey("remediation_plans.id"), index=True, nullable=False)
    step_index = Column(Integer, default=0)
    tool = Column(String, nullable=False)
    args = Column(JSON, default=dict)
    dry_run = Column(Integer, default=0)
    result = Column(JSON, default=dict)
    executed_by = Column(String, default="system")
    idempotency_key = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class Document(Base):
    __tablename__ = "documents"
    id = Column(String, primary_key=True, default=_uuid)
    title = Column(String, nullable=False)
    kind = Column(String, default="runbook")  # runbook | postmortem
    service = Column(String, default="")
    content = Column(Text, default="")
    embedding = Column(JSON, default=list)  # float list; pgvector Vector is the prod migration
    # Phase 7 — owning org (nullable for migration safety; NULL = demo org).
    org_id = Column(String, ForeignKey("organizations.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class EvalRun(Base):
    __tablename__ = "evals"
    id = Column(String, primary_key=True, default=_uuid)
    dataset_version = Column(String, default="golden-v1")
    model = Column(String, default="demo")
    prompt_version = Column(String, default="v1")
    metrics = Column(JSON, default=dict)
    # Phase 7 — owning org (nullable for migration safety; NULL = demo org).
    org_id = Column(String, ForeignKey("organizations.id"), nullable=True, index=True)
    run_at = Column(DateTime, default=_now)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)
