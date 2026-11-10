"""Pydantic request/response schemas — every tool and API input is validated here.

Length limits (security review): every free-text field is capped so a
single request cannot smuggle megabytes of text into the DB, the LLM
prompt, or an incident event. Caps are generous for legitimate use
(runbook content up to 100k, alert titles up to 500 chars) and reject
with a plain 422, never a crash. Severity stays a free string (callers
send P1–P3 and Alertmanager severities such as "critical"); only its
length is capped.
"""
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .config import settings


class NotifyIn(BaseModel):
    """POST /api/incidents/{id}/notify — which channel to notify."""
    channel: Literal["jira", "slack"]


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class SignupIn(BaseModel):
    """POST /api/auth/signup — create a company (org) + its admin user."""
    company_name: str = Field(min_length=1, max_length=128)
    admin_name: str = Field(min_length=1, max_length=128)
    email: str = Field(max_length=254)
    password: str = Field(min_length=8, max_length=256)


class OrgMemberIn(BaseModel):
    """POST /api/orgs/members — invite/add a user to the caller's org."""
    email: str = Field(max_length=254)
    role: Literal["admin", "responder", "viewer"] = "viewer"


class OrgIntegrationIn(BaseModel):
    """Upsert a per-org integration. ``config`` holds NON-secret fields
    only; ``secrets`` holds secret fields (encrypted on storage, never
    returned in plaintext). ``name`` is a plain string (not a Literal)
    on purpose: the PUT handler validates it against INTEGRATION_NAMES
    and answers 400 for an unknown name, including when the body name
    itself is unknown."""
    name: str = Field(max_length=64)
    provider: Optional[str] = Field(default=None, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


class OrgIntegrationOut(BaseModel):
    """Masked view of a per-org integration — secrets never appear here."""
    id: str
    org_id: str
    name: str
    provider: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    secrets_masked: dict[str, str] = Field(default_factory=dict)
    is_active: bool = True
    last_tested_at: Optional[str] = None
    last_test_ok: Optional[bool] = None


class WebhookTokenOut(BaseModel):
    """Returned ONLY at creation / rotation: the full token is shown once
    and never again. ``masked`` is the display form used elsewhere."""
    webhook_token: str
    masked: str


class AlertIn(BaseModel):
    title: str = Field(max_length=500)
    service: str = Field(max_length=128)
    severity: str = Field(default="P2", max_length=16)
    source: str = Field(default="webhook", max_length=64)
    external_id: Optional[str] = Field(default=None, max_length=128)


class SimulateIn(BaseModel):
    scenario: str = Field(default="checkout-oom", max_length=128)


class AlertmanagerAlert(BaseModel):
    """One alert inside a Prometheus Alertmanager webhook payload (Phase 3)."""
    status: str = Field(default="firing", max_length=32)  # firing | resolved
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: Optional[str] = Field(default=None, max_length=64)
    endsAt: Optional[str] = Field(default=None, max_length=64)
    fingerprint: Optional[str] = Field(default=None, max_length=128)
    generatorURL: Optional[str] = Field(default=None, max_length=512)

    @field_validator("labels", "annotations")
    @classmethod
    def _cap_mapping(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 100:
            raise ValueError("at most 100 labels/annotations per alert")
        for k, val in v.items():
            if len(k) > 256 or len(val) > 4096:
                raise ValueError("label/annotation key/value too long")
        return v


class AlertmanagerPayload(BaseModel):
    """Top-level Prometheus Alertmanager webhook payload (Phase 3)."""
    version: Optional[str] = Field(default=None, max_length=32)
    groupKey: Optional[str] = Field(default=None, max_length=256)
    status: Optional[str] = Field(default=None, max_length=32)
    receiver: Optional[str] = Field(default=None, max_length=128)
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    alerts: list[AlertmanagerAlert] = Field(default_factory=list, max_length=100)

    @field_validator("groupLabels", "commonLabels", "commonAnnotations")
    @classmethod
    def _cap_common_mapping(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 100:
            raise ValueError("at most 100 labels/annotations")
        for k, val in v.items():
            if len(k) > 256 or len(val) > 4096:
                raise ValueError("label/annotation key/value too long")
        return v


class PlanEditIn(BaseModel):
    steps: list[dict] = Field(default_factory=list, max_length=50)


class RejectIn(BaseModel):
    reason: str = Field(default="", max_length=1000)


class ExecuteIn(BaseModel):
    dry_run: bool = False
    approval_token: Optional[str] = Field(default=None, max_length=128)


class DocumentIn(BaseModel):
    title: str = Field(max_length=300)
    kind: str = Field(default="runbook", max_length=32)
    service: str = Field(default="", max_length=128)
    content: str = Field(max_length=100_000)


class KnowledgeSearchIn(BaseModel):
    """POST /api/knowledge/search — previously an unvalidated raw dict."""
    query: str = Field(default="", max_length=1000)
    service: str = Field(default="", max_length=128)


def incident_out(i) -> dict:
    # Phase 3 — the linked Jira issue, if one was created. The URL is built
    # from settings only when both a key and a Jira URL are present.
    jira_key = getattr(i, "jira_issue_key", None) or None
    jira_url = None
    if jira_key and (settings.jira_url or "").strip():
        jira_url = f"{settings.jira_url.rstrip('/')}/browse/{jira_key}"
    return {"id": i.id, "title": i.title, "service": i.service, "severity": i.severity,
            "status": i.status, "source": i.source, "scenario": i.scenario or "",
            "jira_issue_key": jira_key, "jira_issue_url": jira_url,
            "opened_at": i.opened_at.isoformat() if i.opened_at else None,
            "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None}
