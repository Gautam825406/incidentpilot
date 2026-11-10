"""MCP-style tools — typed, independently testable, read-only except Executor.

Each tool validates its inputs with Pydantic and returns structured output.
Tools never receive raw model instructions; the model proposes arguments,
the schema decides whether they are legal (PRD §8.4).
"""
import re

from pydantic import BaseModel, Field

from .llm import cosine, hash_embedding
from .security import sanitise_evidence
from .synthetic import POSTMORTEMS, RUNBOOKS, SERVICE_STATE, get_scenario


class SearchLogsInput(BaseModel):
    service: str = Field(max_length=128)
    pattern: str = Field(default="", max_length=1000)
    window_minutes: int = Field(default=15, ge=1, le=1440)


class MetricsInput(BaseModel):
    service: str = Field(max_length=128)


class KnowledgeInput(BaseModel):
    query: str = Field(max_length=1000)
    service: str = Field(default="", max_length=128)
    top_k: int = Field(default=3, ge=1, le=10)


class ExecutorInput(BaseModel):
    tool: str = Field(max_length=64)
    service: str = Field(max_length=128)
    replicas: int = Field(default=2, ge=1, le=10)


# --- Logs MCP (read-only) -------------------------------------------------
def search_logs(inp: SearchLogsInput, scenario: dict) -> dict:
    logs = scenario.get("logs", [])
    if inp.pattern:
        logs = [l for l in logs if inp.pattern.lower() in l.lower()]
    return {
        "tool": "search_logs",
        "service": inp.service,
        "count": len(logs),
        "lines": [sanitise_evidence(l) for l in logs],
    }


def get_error_clusters(inp: SearchLogsInput, scenario: dict) -> dict:
    clusters: dict[str, int] = {}
    for line in scenario.get("logs", []):
        key = "OutOfMemoryError" if "OutOfMemory" in line else (
            "pool exhausted" if "pool" in line.lower() else (
                "certificate expired" if "certificate" in line.lower() else (
                    "cache" if "cache" in line.lower() else "other")))
        clusters[key] = clusters.get(key, 0) + 1
    return {"tool": "get_error_clusters", "service": inp.service, "clusters": clusters}


# --- Metrics MCP (read-only, fixed templates — no raw query from model) ---
def get_metrics(inp: MetricsInput, scenario: dict) -> dict:
    m = scenario.get("metrics", {})
    baseline = SERVICE_STATE.get(inp.service, {})
    return {
        "tool": "get_metrics",
        "service": inp.service,
        "error_rate": m.get("error_rate"),
        "error_rate_baseline": m.get("error_rate_baseline", baseline.get("error_rate")),
        "latency_ms": m.get("latency_ms"),
        "latency_baseline": m.get("latency_baseline", baseline.get("latency_ms")),
        "cpu": m.get("cpu"),
        "anomaly": (m.get("error_rate", 0) or 0) > 5 * (m.get("error_rate_baseline", 0.4) or 0.4),
    }


# --- Knowledge MCP (hybrid RAG: BM25-style keyword + vector cosine) ------
def _bm25_score(query: str, text: str) -> float:
    words = set(re.findall(r"[a-z0-9]+", query.lower()))
    text_words = re.findall(r"[a-z0-9]+", text.lower())
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in text_words)
    return hits / len(words)


def search_knowledge(inp: KnowledgeInput) -> dict:
    docs = RUNBOOKS + POSTMORTEMS
    qvec = hash_embedding(inp.query)
    scored = []
    for d in docs:
        if inp.service and d["service"] != inp.service:
            continue
        text = f"{d['title']} {d['content']}"
        vector_score = cosine(qvec, hash_embedding(text))
        keyword_score = _bm25_score(inp.query, text)
        hybrid = 0.6 * vector_score + 0.4 * keyword_score
        scored.append({**d, "score": round(hybrid, 4), "vector_score": round(vector_score, 4), "keyword_score": round(keyword_score, 4)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"tool": "search_knowledge", "query": inp.query, "results": scored[: inp.top_k]}
