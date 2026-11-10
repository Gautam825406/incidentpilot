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
