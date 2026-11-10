# Architecture

```
                         ┌──────────────────── Prometheus ──► GET /api/v1/query (read-only)
                         │   (cpu, memory, error rate, latency, availability)
Alertmanager ──webhook──►│
  (or legacy webhook /   │   ┌─────────────── Elasticsearch / OpenSearch ──► POST /_search (read-only)
   Simulate button)     ▼   ▼
                    FastAPI ──► Postgres/SQLite (incidents, audit, evidence)
                         │  (async, in-process background task; Celery+Redis is the
                         │   scale-out migration — same task interface)
                         ▼
              LangGraph supervisor (MemorySaver checkpoint)
                 ├─ Triage ──► incident history + integration config
                 ├─ Logs ──► real log provider (ES/OpenSearch) or demo fixtures
                 ├─ Metrics ──► Prometheus or demo fixtures
                 └─ Knowledge ──► hybrid RAG (BM25 + hash-vector cosine, runbooks/postmortems)
                         ▼
              Ranked hypotheses (citation-enforced, 0.65 confidence gate)
                         ▼  human approval (JWT + role, approval token, 30-min expiry)
              Executor — allow-list only, dry-run, idempotency key
                         ▼  (SIMULATED services only)
              Verification (simulated error rate < 1%) ──► Postmortem draft
                         │
                         └─► Grafana dashboard links (per-service URLs, no secrets)
```

## Decisions
- **LangGraph supervisor, not a single ReAct agent:** state, branching, approval interrupts, resume from checkpoint.
- **MCP-style tools:** typed Pydantic inputs, independently testable, read-only except Executor.
- **Postgres + pgvector-ready:** embeddings stored as JSON float lists so SQLite demo and Postgres share code; swap to a `Vector` column in one migration.
- **Groq only for LLM:** `llama-3.3-70b-versatile` via api.groq.com; demo mode is deterministic and labelled.
- **Safety:** allow-list + approval token + expiry + idempotency + dry-run + injection guard + PII redaction. Unsafe-action target: 0.
- **Read-only, optional integrations:** Prometheus (GET only), Elasticsearch/OpenSearch (`_search` only) and Grafana (link-building only, never queried) are all opt-in via env vars. With none configured the app behaves exactly as before. A configured-but-unavailable source yields an `integration_unavailable` evidence item — values are never invented. Both HTTP clients use `httpx trust_env=False` so proxy env vars can never reroute or break internal traffic.
- **Remediation stays simulated-only:** the executor mutates an in-memory `SERVICE_STATE` error rate, never real infrastructure. Real remediation would need a separate, credentialed actuator service with per-target blast-radius limits — deliberately out of scope for the agent loop (see README "Why remediation stays disabled for real infrastructure").
- **Schema migration in `init_db()`:** `create_all` never alters existing tables, so `add_missing_columns()` inspects every mapped table after `create_all` and issues `ALTER TABLE ... ADD COLUMN` for model columns the DB file lacks (e.g. the three Phase 6 `Hypothesis` JSON columns). Idempotent; a `NOT NULL` column with no default on a populated table is skipped, not fatal.

## Integration architecture
- **Prometheus** (`backend/app/integrations/prometheus.py`): `PrometheusClient` — `health()` (`GET /api/v1/status/buildinfo`, never raises), `query()` (PromQL, retries 5xx/timeout twice, fails fast on 4xx/bad payloads), `service_metrics()` (cpu/memory/error-rate/latency/availability fetched concurrently). Label values are PromQL-escaped. NaN/Inf/empty results → `available: false`.
- **Logs** (`backend/app/integrations/logs.py`): provider adapters (`ElasticsearchProvider`, `OpenSearchProvider`, `NoLogProvider`) sharing a `_search` base with retries on 5xx/timeout. `search_logs_real()` returns `{provider, available, entries, groups, error}`; `group_exceptions()` aggregates by exception type; `to_evidence()` converts entries + group summaries to evidence rows. Elasticsearch takes precedence when both are configured.
- **Grafana** (`backend/app/integrations/grafana.py`): pure functions over settings — `dashboard_url(service)` and `grafana_status()`. No HTTP, no secrets.
- **Alertmanager** (`POST /api/webhooks/alert` in `main.py`): payloads with an `alerts` key are validated as `AlertmanagerPayload`; firing alerts become incidents (severity mapped critical→P1, warning→P2, else P3; service from `service`/`job`/`instance` labels), deduped by fingerprint, `resolved` alerts skipped. The legacy `{title, service, …}` shape is handled on the same endpoint. Optional `ALERT_WEBHOOK_SECRET` enforces an `X-Alert-Token` header (constant-time compare).
- **Investigation wiring** (`agents.py` `run_investigation`): in live mode the Logs agent queries the configured provider over the last 60 minutes and the Metrics agent queries Prometheus; in demo mode both use synthetic fixtures labelled `demo/synthetic`. A hypothesis requires usable evidence (everything except `integration_unavailable` items) and confidence ≥ 0.65, otherwise the run abstains. Live-mode guard (Phase 8): if Groq is down and the alert matches no known scenario, the run abstains rather than falling back to a deterministic fixture — see README "Known limitations".
- **Status** (`GET /api/integrations/status`): Prometheus configured/reachable (live check, capped at 5 s), logs provider/configured, Grafana configured/url. Never returns tokens or secrets.

## Local dev
```bash
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && cp .env.example .env
uvicorn app.main:app --reload --port 8000
cd frontend && npm install && npm run dev   # http://localhost:5173
```
Tests: `cd backend && .venv/bin/python -m pytest tests -q` (52 tests, all mocked — no real infra). Eval: `python -m evals.run_eval` (writes `backend/evals/results.json`, demo-labelled unless a Groq key is set).

## Docker
`docker compose up --build` (backend :8000, frontend :5173, Postgres :5432). Optional local Prometheus under the `monitoring` profile — see `prometheus.yml` and the README Docker section; the default `up` is unchanged without the profile.

## Deploy (free)
- Backend: Render free web service, `GROQ_API_KEY` + `DATABASE_URL` (Neon free Postgres) as env vars.
- Frontend: Vercel free, `VITE_API_URL` = Render backend URL.
- Local demo: `docker compose up --build`.
- Production checklist (JWT secret, CORS, webhook secret, integration verification, eval re-run): see README.
