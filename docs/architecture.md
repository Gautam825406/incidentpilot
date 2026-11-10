# Architecture

```
Alert / Simulate ──► FastAPI ──► Postgres/SQLite (incidents, audit, evidence)
                         │  (async, in-process background task; Celery+Redis is the
                         │   scale-out migration — same task interface)
                         ▼
              LangGraph supervisor (MemorySaver checkpoint)
                 ├─ Triage ──► incident history
                 ├─ Logs ──► Logs MCP (read-only, PII-redacted)
                 ├─ Metrics ──► Metrics MCP (fixed templates)
                 └─ Knowledge ──► hybrid RAG (BM25 + hash-vector cosine, runbooks/postmortems)
                         ▼
              Ranked hypotheses (citation-enforced) ──► Remediation plan
                         ▼  human approval (JWT + role, approval token, 30-min expiry)
              Executor MCP — allow-list only, dry-run, idempotency key
                         ▼
              Verification (simulated error rate < 1%) ──► Postmortem draft
```

## Decisions
- **LangGraph supervisor, not a single ReAct agent:** state, branching, approval interrupts, resume from checkpoint.
- **MCP-style tools:** typed Pydantic inputs, independently testable, read-only except Executor.
- **Postgres + pgvector-ready:** embeddings stored as JSON float lists so SQLite demo and Postgres share code; swap to a `Vector` column in one migration.
- **Groq only for LLM:** `llama-3.3-70b-versatile` via api.groq.com; demo mode is deterministic and labelled.
- **Safety:** allow-list + approval token + expiry + idempotency + dry-run + injection guard + PII redaction. Unsafe-action target: 0.

## Deploy (free)
- Backend: Render free web service, `GROQ_API_KEY` + `DATABASE_URL` (Neon free Postgres) as env vars.
- Frontend: Vercel free, `VITE_API_URL` = Render backend URL.
- Local demo: `docker compose up --build`.
