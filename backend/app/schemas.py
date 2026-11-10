"""Pydantic request/response schemas — every tool and API input is validated here."""
from typing import Any, Optional

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    email: str
    password: str


class AlertIn(BaseModel):
    title: str
    service: str
    severity: str = "P2"
    source: str = "webhook"
    external_id: Optional[str] = None


class SimulateIn(BaseModel):
    scenario: str = "checkout-oom"


class AlertmanagerAlert(BaseModel):
    """One alert inside a Prometheus Alertmanager webhook payload (Phase 3)."""
    status: str = "firing"  # firing | resolved
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: Optional[str] = None
    endsAt: Optional[str] = None
    fingerprint: Optional[str] = None
    generatorURL: Optional[str] = None


class AlertmanagerPayload(BaseModel):
    """Top-level Prometheus Alertmanager webhook payload (Phase 3)."""
    version: Optional[str] = None
    groupKey: Optional[str] = None
    status: Optional[str] = None
    receiver: Optional[str] = None
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    alerts: list[AlertmanagerAlert] = Field(default_factory=list)


class PlanEditIn(BaseModel):
    steps: list[dict] = Field(default_factory=list)


class RejectIn(BaseModel):
    reason: str = ""


class ExecuteIn(BaseModel):
    dry_run: bool = False
    approval_token: Optional[str] = None


class DocumentIn(BaseModel):
    title: str
    kind: str = "runbook"
    service: str = ""
    content: str


def incident_out(i) -> dict:
    return {"id": i.id, "title": i.title, "service": i.service, "severity": i.severity,
            "status": i.status, "source": i.source, "scenario": i.scenario or "",
            "opened_at": i.opened_at.isoformat() if i.opened_at else None,
            "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None}
