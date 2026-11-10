"""Groq LLM client — Groq only, model openai/gpt-oss-120b.

Model history: llama-3.3-70b-versatile was retired by Groq for free/developer
tier on 2026-08-16 (Enterprise only after that) and returns HTTP 404 — that
is why live runs fell back to the deterministic path. Groq's own replacement
for it is openai/gpt-oss-120b.

Calls https://api.groq.com/openai/v1/chat/completions with GROQ_API_KEY
from the environment. No key is ever stored in code. Without a key the
caller gets demo_mode=True and must use the deterministic path in agents.py.
Token/cost accounting is approximate and reported honestly as estimated.
"""
import json
import time

import httpx

from .config import settings

# Groq pricing (approx, public list price at time of writing) — used only
# for cost *estimates* shown in the UI; labelled as estimated.
PRICE_PER_1M_INPUT = 0.15
PRICE_PER_1M_OUTPUT = 0.60


class GroqClient:
    def __init__(self):
        self.api_key = settings.groq_api_key
        self.model = settings.groq_model

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def chat_json(self, system: str, user: str, timeout: float = 25.0) -> dict:
        """Ask Groq for a JSON object. Returns {data, tokens, cost_usd, latency_ms, model}."""
        started = time.time()
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(
                f"{settings.groq_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system + "\nReply with valid JSON only."},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            body = resp.json()
        latency_ms = int((time.time() - started) * 1000)
        usage = body.get("usage", {}) or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        cost = (prompt_tokens * PRICE_PER_1M_INPUT + completion_tokens * PRICE_PER_1M_OUTPUT) / 1_000_000
        content = body["choices"][0]["message"]["content"]
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = {"raw": content}
        return {
            "data": data,
            "tokens": prompt_tokens + completion_tokens,
            "cost_usd": round(cost, 6),
            "latency_ms": latency_ms,
            "model": self.model,
        }


def hash_embedding(text: str, dim: int = 128) -> list[float]:
    """Deterministic TF-hash embedding (no model download).

    Used for the demo hybrid-RAG ranking. In production this is replaced by
    sentence-transformers embeddings stored in pgvector — the interface is
    identical (text -> float list), so the swap is one function.
    """
    import hashlib
    import math
    import re

    vec = [0.0] * dim
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(word.encode()).hexdigest(), 16) % dim
        vec[h] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
