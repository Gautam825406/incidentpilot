"""Data model — PRD §9. UUID PKs, created_at/updated_at on every table."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text

from .database import Base


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="viewer")  # admin | responder | viewer
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
    tokens = Column(Integer, default=0)
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
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class EvalRun(Base):
    __tablename__ = "evals"
    id = Column(String, primary_key=True, default=_uuid)
    dataset_version = Column(String, default="golden-v1")
    model = Column(String, default="demo")
    prompt_version = Column(String, default="v1")
    metrics = Column(JSON, default=dict)
    run_at = Column(DateTime, default=_now)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)
