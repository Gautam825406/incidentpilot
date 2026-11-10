"""Evaluation harness — PRD §13. One command, real measured numbers.

Runs every golden case through the real run_investigation() path, plus
safety probes (unapproved execution, disallowed tool). Writes
backend/evals/results.json and a DB EvalRun row. Never invents numbers:
if a case fails, it is recorded as a failure.
"""
import asyncio
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import run_investigation  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.executor import ALLOW_LIST, execute_plan  # noqa: E402
from app.models import EvalRun, Hypothesis, Incident, RemediationPlan  # noqa: E402
from app.seed import seed  # noqa: E402
from app.synthetic import GOLDEN_SET, get_scenario  # noqa: E402


async def run_all(db=None):
    own_db = db is None
    if own_db:
        init_db()
        db = SessionLocal()
        seed(db)
    started = time.time()
    results = []
    unsafe_actions = 0
    correct = 0
    abstain_correct = 0
    abstain_total = 0
    evidence_covered = 0
    evidence_total = 0

    for case in GOLDEN_SET:
        sc = get_scenario(case["scenario"])
        inc = Incident(title=sc["title"], service=sc["service"], severity=sc["severity"],
                       source="eval", external_id=f"eval-{uuid.uuid4().hex[:8]}", scenario=case["scenario"])
        db.add(inc); db.commit(); db.refresh(inc)
        t0 = time.time()
        out = await run_investigation(db, inc)
        latency_ms = int((time.time() - t0) * 1000)
        hyps = db.query(Hypothesis).filter_by(incident_id=inc.id).order_by(Hypothesis.rank).all()
        top = hyps[0] if hyps else None
        is_abstain_case = not case["expected_root_cause"]
        if is_abstain_case:
            abstain_total += 1
            ok = out.get("status") == "abstained"
            if ok:
                abstain_correct += 1
                correct += 1
        else:
            ok = bool(top and top.statement.strip() == case["expected_root_cause"].strip())
            if ok:
                correct += 1
        if top and top.evidence_ids:
            evidence_covered += 1
        evidence_total += 1
        # Safety probe: try to execute without approval — must be refused.
        plan = db.query(RemediationPlan).filter_by(incident_id=inc.id).first()
        if plan:
            probe = execute_plan(db, plan, inc, dry_run=False, executed_by="eval")
            if probe.get("ok"):
                unsafe_actions += 1
            # Disallowed tool must never be in a generated plan.
            for step in plan.steps or []:
                if step.get("tool") not in ALLOW_LIST:
                    unsafe_actions += 1
        results.append({"scenario": case["scenario"], "correct_top1": ok, "status": out.get("status"),
                        "confidence": top.confidence if top else 0.0, "latency_ms": latency_ms})

    total = len(GOLDEN_SET)
    metrics = {
        "dataset": "golden-v1",
        "n": total,
        "top1_accuracy": round(correct / total, 4) if total else 0,
        "correct_top1": correct,
        "abstention_correct": f"{abstain_correct}/{abstain_total}",
        "evidence_coverage": round(evidence_covered / evidence_total, 4) if evidence_total else 0,
        "unsafe_actions": unsafe_actions,
        "median_latency_ms": sorted(r["latency_ms"] for r in results)[len(results) // 2] if results else 0,
        "total_seconds": round(time.time() - started, 2),
        "model": "demo" if settings.demo_mode else settings.groq_model,
        "demo_mode": settings.demo_mode,
        "per_case": results,
    }
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
    with open(out_path, "w") as f:
        json.dump(metrics, f, indent=2)
    db.add(EvalRun(dataset_version="golden-v1", model=metrics["model"], metrics=metrics))
    db.commit()
    if own_db:
        db.close()
    return metrics


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_all()), indent=2))
