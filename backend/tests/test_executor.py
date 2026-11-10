import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db
from app.executor import execute_plan, verify_recovery
from app.models import Incident, RemediationPlan
from app.synthetic import SERVICE_STATE


def _setup():
    import uuid as _uuid
    init_db()
    db = SessionLocal()
    inc = Incident(title="t", service="checkout-api", severity="P1", external_id=f"test-exec-{_uuid.uuid4().hex[:8]}")
    db.add(inc); db.commit(); db.refresh(inc)
    return db, inc


def test_unapproved_execution_refused():
    db, inc = _setup()
    plan = RemediationPlan(incident_id=inc.id, steps=[{"tool": "restart_service", "args": {}}], status="pending")
    db.add(plan); db.commit()
    assert execute_plan(db, plan, inc, dry_run=False, executed_by="test")["ok"] is False


def test_disallowed_tool_blocked_and_idempotent():
    db, inc = _setup()
    plan = RemediationPlan(incident_id=inc.id, steps=[{"tool": "delete_database", "args": {}}], status="approved", approval_token="test-token")
    db.add(plan); db.commit()
    res = execute_plan(db, plan, inc, dry_run=False, executed_by="test")
    assert res["ok"] is True
    assert res["results"][0]["result"]["blocked"] is True
    # Second run replays idempotently — no duplicate execution rows.
    res2 = execute_plan(db, plan, inc, dry_run=False, executed_by="test")
    assert res2["results"][0]["idempotent_replay"] is True


def test_dry_run_changes_nothing_and_verify_passes_after_restart():
    db, inc = _setup()
    SERVICE_STATE["checkout-api"]["error_rate"] = 18.7
    plan = RemediationPlan(incident_id=inc.id, steps=[{"tool": "restart_service", "args": {}}], status="approved", approval_token="test-token")
    db.add(plan); db.commit()
    dry = execute_plan(db, plan, inc, dry_run=True, executed_by="test")
    assert SERVICE_STATE["checkout-api"]["error_rate"] == 18.7
    assert dry["results"][0]["result"]["dry_run"] is True
    execute_plan(db, plan, inc, dry_run=False, executed_by="test")
    assert verify_recovery(db, inc)["passed"] is True
