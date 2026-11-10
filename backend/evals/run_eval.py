"""Evaluation harness — PRD §13. One command, real measured numbers.

Runs every golden case through the real run_investigation() path, plus
safety probes (unapproved execution, disallowed tool). Writes
backend/evals/results.json and a DB EvalRun row. Never invents numbers:
if a case fails, it is recorded as a failure.
"""
import asyncio
import json
import os
import re
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
from app.telemetry import emit_scenario_telemetry  # noqa: E402

# ---------------------------------------------------------------------------
# Semantic matching (Phase 5 Step 2) — deterministic keyword/mechanism
# overlap, scored ALONGSIDE exact-string matching (which is kept as the
# secondary metric). Live eval run dbd04041 (gpt-oss-120b, n=6) scored
# exact-match top-1 0/6 even though the LLM was semantically right in 3/5
# root-cause cases: it worded the same mechanism differently from the
# fixture sentence. Exact-string equality therefore under-reports a live
# model; semantic matching credits a paraphrase of the SAME mechanism
# and never credits a DIFFERENT mechanism.
#
# How it works: each of the five golden mechanisms has (a) STRONG phrases
# — one hit alone is enough, e.g. "connection pool" or "runaway batch" —
# and (b) WEAK single keywords — at least TWO must be present, e.g.
# "certificate" + "expired". Text is lower-cased, split on non-alphanumerics
# and lightly singularised ("misses" -> "miss", "connections" ->
# "connection") before matching. A hypothesis is scored correct iff the
# mechanism classified from its statement equals the mechanism classified
# from the expected root cause. An exact string match always counts.
# An abstain/unknown case has no expected mechanism and is NOT scored as a
# root-cause hit (semantic_correct_top1 is None for it, and it is excluded
# from the semantic denominator). No LLM judge is used: the deterministic
# scorer is the primary (and only) semantic metric, so semantic results
# are fully reproducible and cost no extra Groq calls.
# ---------------------------------------------------------------------------

MECHANISMS = {
    "memory_leak": {
        "label": "memory leak / OOM",
        "strong": ["memory leak", "outofmemory", "out of memory", "heap exhaustion",
                   "heap space", "gc thrashing", "memory exhaustion"],
        "weak": ["memory", "heap", "leak", "oom", "gc", "outofmemory"],
    },
    "pool_exhaustion": {
        "label": "connection pool exhaustion",
        "strong": ["connection pool", "pool exhausted", "pool exhaustion",
                   "pool saturated", "hikari"],
        "weak": ["pool", "connection", "exhausted", "exhaustion", "saturated", "hikari"],
    },
    "cache_miss_storm": {
        "label": "cache miss storm",
        "strong": ["cache miss", "cache storm", "cache flush", "hit rate"],
        "weak": ["cache", "miss", "flush", "storm"],
    },
    "tls_expired": {
        "label": "TLS certificate expired",
        "strong": ["certificate expired", "cert expired", "certificate has expired",
                   "tls certificate", "certificate expiry", "ssl handshake"],
        "weak": ["certificate", "cert", "tls", "ssl", "expired", "expiry", "handshake"],
    },
    "cpu_saturation": {
        "label": "CPU saturation",
        "strong": ["cpu saturation", "cpu saturated", "runaway batch", "cpu above 90"],
        "weak": ["cpu", "saturating", "saturated", "saturation", "batch", "runaway"],
    },
}

# A mechanism is detected with >= 1 strong hit, or >= 2 weak hits.
# Score = 2 * strong hits + weak hits; the highest-scoring mechanism wins.
# A tie at the top is ambiguous -> no mechanism (never guess).
STRONG_WEIGHT = 2
WEAK_HITS_REQUIRED = 2


def _stem(token: str) -> str:
    if token.endswith("es") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
        return token[:-1]
    return token


def _normalize(text: str) -> str:
    return " ".join(_stem(w) for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if w)


def _tokens(text: str) -> set:
    return set(_normalize(text).split())


def _mechanism_scores(text: str) -> dict:
    """Per-mechanism (score, matched keywords) for a statement."""
    flat = " ".join(w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if w)
    toks = _tokens(text)
    out = {}
    for key, spec in MECHANISMS.items():
        strong_hits = [p for p in spec["strong"] if p in flat or p in _normalize(text)]
        weak_hits = [w for w in spec["weak"] if _stem(w) in toks]
        score = STRONG_WEIGHT * len(strong_hits) + len(weak_hits)
        detected = bool(strong_hits) or len(weak_hits) >= WEAK_HITS_REQUIRED
        out[key] = {"score": score, "detected": detected,
                    "matched": sorted(set(strong_hits + weak_hits))}
    return out


def classify_mechanism(text: str):
    """Mechanism key for a statement, or None if none / ambiguous."""
    if not (text or "").strip():
        return None
    scores = _mechanism_scores(text)
    detected = {k: v for k, v in scores.items() if v["detected"]}
    if not detected:
        return None
    best = max(v["score"] for v in detected.values())
    winners = [k for k, v in detected.items() if v["score"] == best]
    return winners[0] if len(winners) == 1 else None


def semantic_match(predicted: str, expected: str) -> dict:
    """Deterministic semantic judgement for one root-cause case.

    Returns {"correct", "predicted_mechanism", "expected_mechanism",
    "matched_keywords", "method"}. An exact (normalised) string match
    always counts. If the expected root cause has no mechanism (abstain /
    unknown case) the case is not scored: correct is False and both
    mechanisms are None — callers must exclude it from the denominator.
    """
    pred_mech = classify_mechanism(predicted)
    exp_mech = classify_mechanism(expected)
    exact = bool((predicted or "").strip()) and (predicted or "").strip().lower() == (expected or "").strip().lower()
    correct = bool(exact) or (pred_mech is not None and pred_mech == exp_mech)
    matched = []
    if pred_mech:
        matched = _mechanism_scores(predicted)[pred_mech]["matched"]
    return {"correct": correct, "predicted_mechanism": pred_mech,
            "expected_mechanism": exp_mech, "matched_keywords": matched,
            "method": "deterministic-keyword-overlap"}


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
    # Semantic top-1 (deterministic keyword/mechanism overlap). Denominator
    # is root-cause cases only: the abstain case has no expected mechanism
    # and is never scored as a root-cause hit.
    semantic_correct = 0
    semantic_total = 0
    total_tokens = 0
    total_cost_usd = 0.0

    for case in GOLDEN_SET:
        sc = get_scenario(case["scenario"])
        inc = Incident(title=sc["title"], service=sc["service"], severity=sc["severity"],
                       source="eval", external_id=f"eval-{uuid.uuid4().hex[:8]}", scenario=case["scenario"])
        db.add(inc); db.commit(); db.refresh(inc)
        # Phase 6 — emit this case's telemetry BEFORE the investigation
        # runs, tagged with the incident's external_id (the run_id the
        # agents scope their reads to), so cases cannot leak signals into
        # each other. Only the scenario's logs/metrics are emitted.
        emit_scenario_telemetry(sc, inc.external_id)
        t0 = time.time()
        out = await run_investigation(db, inc)
        latency_ms = int((time.time() - t0) * 1000)
        hyps = db.query(Hypothesis).filter_by(incident_id=inc.id).order_by(Hypothesis.rank).all()
        top = hyps[0] if hyps else None
        top_statement = top.statement if top else ""
        case_tokens = int(out.get("tokens") or 0)
        case_cost = float(out.get("cost_usd") or 0.0)
        total_tokens += case_tokens
        total_cost_usd += case_cost
        is_abstain_case = not case["expected_root_cause"]
        sem = None
        sem_ok = None
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
            # Semantic (deterministic) alongside exact: a paraphrase of the
            # same mechanism counts; a different mechanism does not.
            sem = semantic_match(top_statement, case["expected_root_cause"])
            sem_ok = sem["correct"]
            semantic_total += 1
            if sem_ok:
                semantic_correct += 1
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
        results.append({"scenario": case["scenario"], "correct_top1": ok,
                        "exact_correct_top1": ok,
                        "semantic_correct_top1": sem_ok,
                        "semantic_method": "deterministic-keyword-overlap" if sem else None,
                        "semantic_llm_judged": None,
                        "predicted_mechanism": sem["predicted_mechanism"] if sem else None,
                        "expected_mechanism": sem["expected_mechanism"] if sem else None,
                        "semantic_matched_keywords": sem["matched_keywords"] if sem else [],
                        "top_hypothesis": top_statement or None,
                        "expected_root_cause": case["expected_root_cause"] or None,
                        "status": out.get("status"),
                        "confidence": top.confidence if top else 0.0,
                        "evidence_covered": bool(top and top.evidence_ids),
                        "latency_ms": latency_ms,
                        "tokens": case_tokens, "cost_usd": round(case_cost, 6),
                        "model": "demo" if settings.demo_mode else settings.groq_model,
                        "demo_mode": settings.demo_mode})

    total = len(GOLDEN_SET)
    metrics = {
        "dataset": "golden-v1",
        "n": total,
        # Exact-string top-1 is kept as the secondary metric; "top1_accuracy"
        # / "correct_top1" keep their historical exact-match meaning so
        # existing consumers (/api/evals/results, /api/evals/run) are unchanged.
        "top1_accuracy": round(correct / total, 4) if total else 0,
        "correct_top1": correct,
        "exact_top1_accuracy": round(correct / total, 4) if total else 0,
        "exact_correct_top1": correct,
        # Semantic top-1 (deterministic keyword/mechanism overlap) is the
        # primary root-cause metric; denominator is root-cause cases only.
        "semantic_top1_accuracy": round(semantic_correct / semantic_total, 4) if semantic_total else 0,
        "semantic_correct_top1": semantic_correct,
        "semantic_top1_total": semantic_total,
        "semantic_method": "deterministic-keyword-overlap",
        "semantic_llm_judged": None,
        "abstention_correct": f"{abstain_correct}/{abstain_total}",
        "evidence_coverage": round(evidence_covered / evidence_total, 4) if evidence_total else 0,
        "unsafe_actions": unsafe_actions,
        "median_latency_ms": sorted(r["latency_ms"] for r in results)[len(results) // 2] if results else 0,
        "total_seconds": round(time.time() - started, 2),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost_usd, 6),
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
