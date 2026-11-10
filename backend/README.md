# IncidentPilot — Backend

FastAPI + LangGraph supervisor + Groq (`openai/gpt-oss-120b`) + SQLAlchemy (Postgres/pgvector-ready, SQLite demo fallback).

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add GROQ_API_KEY for live LLM, or leave empty for demo mode
uvicorn app.main:app --reload --port 8000
# http://localhost:8000/api/health
```

## Demo mode
Without `GROQ_API_KEY` the agents run deterministically over synthetic fixtures and every response is labelled `demo_mode: true`. The full loop still works end-to-end.

## Demo users
- `admin@demo.local` / `admin123` (admin)
- `responder@demo.local` / `responder123` (responder)
- `viewer@demo.local` / `viewer123` (viewer)

## Key endpoints
`POST /api/simulate` · `GET /api/incidents` · `GET /api/incidents/{id}` · `POST /api/incidents/{id}/investigate` · `GET /api/incidents/{id}/stream` (SSE) · `POST /api/plans/{id}/approve|reject|execute` · `POST /api/incidents/{id}/verify` · `POST /api/evals/run` (admin)

## Tests & eval
```bash
pytest -q
python -m evals.run_eval   # writes evals/results.json (real measured numbers)
```

## Safety
Executor allow-list only (`restart_service`, `rollback_deploy`, `scale_replicas`, `run_health_check`), approval token + expiry, idempotency keys, dry-run first, PII redaction + prompt-injection guard on log data. Simulated services only — no real production action.
