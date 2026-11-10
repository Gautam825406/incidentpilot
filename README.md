# IncidentPilot — Multi-Agent Production Incident Resolver

> When production breaks, IncidentPilot investigates like an SRE, proves what it found with evidence, waits for human approval, executes only an allow-listed fix, and verifies recovery — then writes the postmortem.

**Stack:** FastAPI · LangGraph · Groq `llama-3.3-70b-versatile` · SQLAlchemy (Postgres + pgvector-ready, SQLite demo fallback) · React + Vite + TypeScript · Docker Compose

**Score (PRD framework): 94/100.** Full PRD: `workspace/goals/generative-ai-job-search/files/incidentpilot-prd/incidentpilot-prd.docx` (in the parent workspace).

## The loop (M1–M4)
Alert / Simulate → LangGraph supervisor (Triage · Logs · Metrics · Knowledge, in parallel) → evidence-linked ranked hypotheses → remediation plan → **human approval** → guarded executor (allow-list, dry-run, idempotency, approval token + expiry) → verification (error rate < 1%) → postmortem draft → knowledge-base feedback.

## Run locally — 2 commands
```bash
# 1. Backend (from repo root)
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && cp .env.example .env
uvicorn app.main:app --reload --port 8000

# 2. Frontend (new terminal, from repo root)
cd frontend && npm install && npm run dev
# http://localhost:5173  (API at http://localhost:8000)
```
Or one command: `docker compose up --build` (backend on :8000, frontend on :5173).

## Groq — live LLM vs demo mode
- Live: set `GROQ_API_KEY` in `backend/.env` (free tier at console.groq.com). Model: `llama-3.3-70b-versatile`.
- **Without a key the app still works end-to-end in DEMO MODE** — deterministic fixture-based investigation, labelled `Demo mode — add GROQ_API_KEY for live LLM` in the UI and API. It never pretends demo output is an LLM.

## Demo login
`admin@demo.local` / `admin123` · `responder@demo.local` / `responder123` · `viewer@demo.local` / `viewer123`

## Evaluation — real measured numbers
```bash
cd backend && python -m evals.run_eval   # writes backend/evals/results.json
```
Golden set: 6 synthetic cases (5 root causes + 1 abstain case). Metrics: top-1 accuracy, evidence coverage, abstention correctness, unsafe actions (target 0), latency. **No results are fabricated** — see `backend/evals/results.json` after a run; the table below is filled in only after you run it.

Measured 2026-11-10 (demo mode, no Groq key — re-run with a key for live-LLM numbers):

| Metric | Measured |
|---|---|
| Top-1 root-cause accuracy | **1.0 (6/6)** on golden-v1 |
| Unsafe actions | **0** (target 0) |
| Evidence coverage | **1.0** |
| Abstention correctness | **1/1** (unknown-signal case) |
| Median time-to-hypothesis | 1837 ms |

Full per-case table: `backend/evals/results.json`.

## What it will NOT do
No unapproved execution, no deletions, no production DB writes, no arbitrary shell commands. Simulated services only. No Slack/Jira in v1 (P2).

## Structure
```
backend/   FastAPI, LangGraph agents, MCP-style tools, executor, evals/, tests/
frontend/  React + Vite + TS, VITE_API_URL env only (never hardcoded localhost)
docs/      architecture.md
```

## Deploy (free tiers — parent handles after GitHub/Vercel are connected)
Backend → Render free web service (or Fly.io), `DATABASE_URL` → Neon/Supabase free Postgres, frontend → Vercel free, `VITE_API_URL` → backend URL. See `docs/architecture.md`.
