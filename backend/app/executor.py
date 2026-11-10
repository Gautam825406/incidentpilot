"""Guarded executor (PRD §6 FR-09/FR-10).

Rules enforced here, nowhere else:
- Only allow-listed tools, only after approval, only with a valid,
  unexpired approval token.
- Idempotency: (plan_id, step_index) is unique — re-running a step returns
  the original result, never double-applies it.
- Dry-run first: dry-run changes nothing.
- Simulated services only: SERVICE_STATE error rates are the verification
  signal. No real production action is ever taken.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import ActionExecution, Incident, IncidentEvent, RemediationPlan
from .synthetic import SERVICE_STATE

ALLOW_LIST = {"restart_service", "rollback_deploy", "scale_replicas", "run_health_check"}
# What each tool does to the simulated error rate.
EFFECT = {"restart_service": 0.8, "rollback_deploy": 0.4, "scale_replicas": 1.2, "run_health_check": None}


def _event(db: Session, incident_id: str, type_: str, actor: str, payload: dict):
    db.add(IncidentEvent(incident_id=incident_id, type=type_, actor=actor, payload=payload))
    db.commit()


def execute_plan(db: Session, plan: RemediationPlan, incident: Incident, dry_run: bool, executed_by: str) -> dict:
    if plan.approval_token is None:
        return {"ok": False, "error": "No approval token — execution refused"}
    if plan.status not in ("approved", "executed"):
        return {"ok": False, "error": f"Plan is {plan.status}, not approved — execution refused"}
    if plan.status == "executed" and not dry_run:
        # Idempotent replay: return the stored live results, never re-apply.
        prior = db.query(ActionExecution).filter_by(plan_id=plan.id, dry_run=0).order_by(ActionExecution.step_index).all()
        return {"ok": True, "dry_run": False, "idempotent_replay": True, "plan_id": plan.id,
                "results": [{"step": a.step_index, "tool": a.tool, "result": a.result, "idempotent_replay": True} for a in prior]}
    if plan.expires_at and plan.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        plan.status = "expired"
        db.commit()
        return {"ok": False, "error": "Plan expired — execution refused, re-approval required"}

    results = []
    for i, step in enumerate(plan.steps or []):
        tool = step.get("tool", "")
        # Dry-run and live execution have separate idempotency keys: a dry-run
        # must never consume the live execution (that was a real bug).
        key = f"{plan.id}:{i}:{'dry' if dry_run else 'live'}"
        existing = db.query(ActionExecution).filter_by(idempotency_key=key).first()
        if existing:
            results.append({"step": i, "tool": tool, "result": existing.result, "idempotent_replay": True})
            continue
        if tool not in ALLOW_LIST:
            _event(db, incident.id, "execution", "executor", {"message": f"BLOCKED disallowed tool: {tool}", "tool": tool})
            blocked = {"blocked": True, "reason": f"{tool} is not in the allow-list"}
            # Record the blocked attempt too — the audit trail must show it.
            db.add(ActionExecution(plan_id=plan.id, step_index=i, tool=tool, args=step.get("args", {}),
                                   dry_run=1 if dry_run else 0, result=blocked, executed_by=executed_by, idempotency_key=key))
            db.commit()
            results.append({"step": i, "tool": tool, "result": blocked, "idempotent_replay": False})
            continue
        if dry_run:
            result = {"dry_run": True, "would_run": tool, "args": step.get("args", {}), "service": incident.service}
        else:
            effect = EFFECT.get(tool)
            if effect is not None:
                SERVICE_STATE.setdefault(incident.service, {})["error_rate"] = effect
            result = {"ok": True, "tool": tool, "service": incident.service,
                      "error_rate_after": SERVICE_STATE.get(incident.service, {}).get("error_rate")}
        db.add(ActionExecution(plan_id=plan.id, step_index=i, tool=tool, args=step.get("args", {}),
                               dry_run=1 if dry_run else 0, result=result, executed_by=executed_by, idempotency_key=key))
        db.commit()
        results.append({"step": i, "tool": tool, "result": result, "idempotent_replay": False})

    if not dry_run:
        plan.status = "executed"
        incident.status = "verifying"
        db.commit()
        _event(db, incident.id, "execution", executed_by, {"message": "Plan executed", "results": results})
    return {"ok": True, "dry_run": dry_run, "results": results, "plan_id": plan.id}


def verify_recovery(db: Session, incident: Incident) -> dict:
    """Verification: error rate below 1% for the simulated service."""
    rate = SERVICE_STATE.get(incident.service, {}).get("error_rate", 99.0)
    passed = rate < 1.0
    incident.status = "resolved" if passed else "verifying"
    if passed:
        incident.resolved_at = datetime.now(timezone.utc)
    db.commit()
    _event(db, incident.id, "verification", "executor",
           {"message": f"Verification {'PASSED' if passed else 'FAILED'} — error rate {rate}% (threshold < 1%)", "error_rate": rate, "passed": passed})
    return {"passed": passed, "error_rate": rate, "threshold": 1.0,
            "message": "Recovery verified — error rate back to baseline." if passed else "Verification failed — error rate still above threshold. Rollback suggested."}


def postmortem_draft(incident: Incident, hypotheses: list, plan: RemediationPlan | None) -> str:
    lines = [
        f"# Postmortem — {incident.title}",
        "",
        f"Service: {incident.service} | Severity: {incident.severity} | Status: {incident.status}",
        "",
        "## What happened",
        hypotheses[0].statement if hypotheses else "Investigation abstained — insufficient evidence.",
        "",
        "## Root cause",
        hypotheses[0].statement if hypotheses else "Unknown — see manual checks.",
        "",
        "## Remediation",
    ]
    if plan:
        for s in plan.steps or []:
            lines.append(f"- {s.get('tool')}: {s.get('description')}")
        lines.append(f"\nRollback plan: {plan.rollback}")
    lines += ["", "## Follow-ups", "- Add a canary + heap/pool alert for this service.", "- Review the runbook cited in the investigation."]
    return "\n".join(lines)
