"""Phase 5 Step 2 tests — semantic evaluation.

Covers: the deterministic keyword/mechanism overlap scorer in
evals/run_eval.py (a paraphrase of the same mechanism counts, a different
mechanism does not, an exact match still counts, and the unknown/abstain
case is never scored as a root-cause hit), and the eval runner recording
BOTH exact and semantic top-1 (plus tokens/cost) per case, in results.json
and in the EvalRun DB row. All mocked / local — no real Groq calls.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_phase5_semantic.db"
for _f in ("test_phase5_semantic.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import pytest  # noqa: E402

import evals.run_eval as run_eval  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.models import EvalRun, Hypothesis  # noqa: E402
from app.synthetic import SCENARIOS  # noqa: E402
from evals.run_eval import classify_mechanism, semantic_match  # noqa: E402

# Paraphrases of the five golden root causes: same mechanism, different
# wording from the fixture sentence (the live-eval failure mode from run
# dbd04041, where exact-string matching scored 0/6 on these).
PARAPHRASES = {
    "checkout-oom": "Checkout keeps crashing after the new deploy — the service runs "
                    "out of memory, heap usage climbs until it throws OutOfMemoryError",
    "payments-pool": "Payments API cannot get a database connection; the connection "
                     "pool is exhausted and requests time out",
    "search-cache": "After the cache was flushed, search is slow because every request "
                    "misses the cache and hammers the database",
    "auth-cert": "Auth logins are failing because the TLS certificate has expired",
    "inventory-cpu": "A runaway batch job is saturating the CPU on inventory-api",
}


def test_paraphrase_same_mechanism_counts():
    for key, paraphrase in PARAPHRASES.items():
        res = semantic_match(paraphrase, SCENARIOS[key]["root_cause"])
        assert res["correct"] is True, (key, res)
        assert res["predicted_mechanism"] == res["expected_mechanism"]
        assert res["method"] == "deterministic-keyword-overlap"
        assert res["matched_keywords"]


def test_different_mechanism_does_not_count():
    keys = list(PARAPHRASES)
    for key in keys:
        for other in keys:
            if other == key:
                continue
            res = semantic_match(PARAPHRASES[key], SCENARIOS[other]["root_cause"])
            assert res["correct"] is False, (key, other, res)
            assert res["predicted_mechanism"] != res["expected_mechanism"]


def test_exact_match_still_counts():
    for key, sc in SCENARIOS.items():
        if not sc["root_cause"]:
            continue
        res = semantic_match(sc["root_cause"], sc["root_cause"])
        assert res["correct"] is True, (key, res)


def test_unknown_abstain_case_is_not_a_root_cause_hit():
    res = semantic_match("Insufficient evidence to determine a root cause.", "")
    assert res["correct"] is False
    assert res["expected_mechanism"] is None
    assert res["predicted_mechanism"] is None
    assert classify_mechanism("") is None
    assert classify_mechanism("something feels slow") is None


def test_no_mechanism_statement_does_not_count():
    res = semantic_match("Something is slow and users are unhappy",
                         SCENARIOS["checkout-oom"]["root_cause"])
    assert res["correct"] is False
    assert res["predicted_mechanism"] is None


def test_ambiguous_statement_with_two_mechanisms_does_not_count():
    # "Memory leak" and "CPU saturation" score equally (strong 2 + weak 2
    # each): a tie -> ambiguous -> no mechanism, never a guess.
    res = semantic_match("Memory leak and CPU saturation",
                         SCENARIOS["checkout-oom"]["root_cause"])
    assert res["predicted_mechanism"] is None
    assert res["correct"] is False


@pytest.mark.asyncio
async def test_runner_records_both_exact_and_semantic(monkeypatch):
    results_path = os.path.join(os.path.dirname(run_eval.__file__), "results.json")
    with open(results_path, "rb") as f:
        original_results = f.read()
    try:
        init_db()
        db = SessionLocal()

        async def fake_run_investigation(fake_db, inc):
            fake_db.add(Hypothesis(
                incident_id=inc.id, rank=1,
                statement=PARAPHRASES.get(inc.scenario, "Insufficient evidence to determine a root cause."),
                confidence=0.9,
                status="abstained" if inc.scenario == "unknown-signal" else "proposed",
                evidence_ids=["ev1"]))
            fake_db.commit()
            return {"status": "abstained" if inc.scenario == "unknown-signal" else "awaiting_approval",
                    "tokens": 100, "cost_usd": 0.001}

        monkeypatch.setattr(run_eval, "run_investigation", fake_run_investigation)
        metrics = await run_eval.run_all(db)

        # Exact: only the abstain case matches its fixture wording (1/6).
        assert metrics["correct_top1"] == 1
        assert metrics["top1_accuracy"] == pytest.approx(1 / 6, abs=0.001)
        assert metrics["exact_correct_top1"] == 1
        # Semantic: all five paraphrased root causes count (5/5).
        assert metrics["semantic_correct_top1"] == 5
        assert metrics["semantic_top1_total"] == 5
        assert metrics["semantic_top1_accuracy"] == 1.0
        assert metrics["semantic_method"] == "deterministic-keyword-overlap"
        assert metrics["semantic_llm_judged"] is None
        assert metrics["abstention_correct"] == "1/1"
        assert metrics["total_tokens"] == 600
        assert metrics["total_cost_usd"] == pytest.approx(0.006)
        assert metrics["model"] == "demo" or isinstance(metrics["model"], str)
        assert "demo_mode" in metrics

        by_scenario = {c["scenario"]: c for c in metrics["per_case"]}
        assert len(by_scenario) == 6
        for key in PARAPHRASES:
            case = by_scenario[key]
            assert case["correct_top1"] is False  # exact wording differs
            assert case["semantic_correct_top1"] is True
            assert case["predicted_mechanism"] == case["expected_mechanism"]
            assert case["tokens"] == 100
            assert case["cost_usd"] == pytest.approx(0.001)
            assert case["expected_root_cause"] == SCENARIOS[key]["root_cause"]
        abstain = by_scenario["unknown-signal"]
        assert abstain["correct_top1"] is True
        assert abstain["semantic_correct_top1"] is None  # never scored as a hit
        assert abstain["expected_mechanism"] is None

        # results.json on disk carries both metrics ...
        with open(results_path) as f:
            on_disk = json.load(f)
        assert on_disk["semantic_correct_top1"] == 5
        assert on_disk["correct_top1"] == 1
        # ... and so does the EvalRun DB row.
        row = db.query(EvalRun).order_by(EvalRun.run_at.desc()).first()
        assert row is not None
        assert row.metrics["semantic_correct_top1"] == 5
        assert row.metrics["semantic_top1_accuracy"] == 1.0
        assert row.metrics["correct_top1"] == 1
        assert row.metrics["total_tokens"] == 600
        db.close()
    finally:
        with open(results_path, "wb") as f:
            f.write(original_results)
