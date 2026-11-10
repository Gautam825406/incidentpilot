# Architecture

```
                         ┌──────────────────── Prometheus ──► GET /api/v1/query (read-only)
                         │   (cpu, memory, error rate, latency, availability)
Alertmanager ──webhook──►│
  (or legacy webhook /   │   ┌─────────────── Elasticsearch / OpenSearch ──► POST /_search (read-only)
   Simulate button)     ▼   ▼
                    FastAPI ──► Postgres (primary) / SQLite (local/demo fallback)
                         │  (in-process worker queue for webhook investigations;
                         │   Celery+Redis is the scale-out migration — same task interface)
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
                         ├─► Grafana dashboard links (per-service URLs, no secrets)
                         ├─► Jira issue — created on demand (admin/responder), severity → priority
                         └─► Slack notification — on demand + best-effort on investigation/resolution
```

## Decisions
- **LangGraph supervisor, not a single ReAct agent:** state, branching, approval interrupts, resume from checkpoint.
- **MCP-style tools:** typed Pydantic inputs, independently testable, read-only except Executor.
- **Postgres is the primary DB path (Phase 4):** `psycopg2-binary` is in requirements.txt; `DATABASE_URL` accepts `postgresql://` and `postgres://` (normalised in `database.py`). SQLite is the local/demo fallback only — same SQLAlchemy models, zero setup, used by the default URL and the test suite. Embeddings stored as JSON float lists so SQLite demo and Postgres share code; swap to a `Vector` column in one migration. Neon/Supabase free-tier setup: see README "Postgres setup".
- **Background worker (Phase 4):** webhook investigations run in an in-process asyncio queue (`app/worker.py` — `enqueue_investigation(incident_id)`, single consumer, own DB session per investigation, best-effort Slack notification on outcome). The webhook returns immediately with the same response shape; `/api/simulate` stays synchronous because its contract returns the investigation inline. The queue is in-memory and single-process (lost on restart, no cross-worker sharing) — Celery + Redis is the scale-out path behind the same `enqueue_investigation` interface.
- **Security hardening (Phase 4):** new passwords get a per-user random salt stored as `salt$hash` (`security.py`); legacy fixed-salt demo hashes still verify. The app refuses to start in live mode when `JWT_SECRET` is still the demo default (`config.validate_production_settings`, env-driven at import; demo mode only warns). CORS is an env allow-list (`CORS_ORIGINS`, default localhost:5173/3000 + the production Vercel frontend; `"*"` only when explicitly set). In-memory sliding-window rate limiting on login (10/min/IP) and the alert webhook (60/min/IP), 429 with a generic message; per-process only, reset between tests by `tests/conftest.py`, disable with `RATE_LIMIT_ENABLED=false`. Auth is still demo-grade — see README "Known limitations".
- **Groq only for LLM:** `openai/gpt-oss-120b` via api.groq.com; demo mode is deterministic and labelled.
- **Safety:** allow-list + approval token + expiry + idempotency + dry-run + injection guard + PII redaction. Unsafe-action target: 0.
- **Read-only, optional integrations:** Prometheus (GET only), Elasticsearch/OpenSearch (`_search` only) and Grafana (link-building only, never queried) are all opt-in via env vars. With none configured the app behaves exactly as before. A configured-but-unavailable source yields an `integration_unavailable` evidence item — values are never invented. Both HTTP clients use `httpx trust_env=False` so proxy env vars can never reroute or break internal traffic. Jira and Slack are the deliberate exception to read-only (Phase 3): they write, but only when configured *and* explicitly triggered by an admin/responder via `POST /api/incidents/{id}/notify`, plus a best-effort Slack post on investigation outcome / resolution that is a no-op when unconfigured and can never fail the incident flow.
- **Remediation stays simulated-only:** the executor mutates an in-memory `SERVICE_STATE` error rate, never real infrastructure. Real remediation would need a separate, credentialed actuator service with per-target blast-radius limits — deliberately out of scope for the agent loop (see README "Why remediation stays disabled for real infrastructure").
- **Schema migration in `init_db()`:** `create_all` never alters existing tables, so `add_missing_columns()` inspects every mapped table after `create_all` and issues `ALTER TABLE ... ADD COLUMN` for model columns the DB file lacks (e.g. the three Phase 6 `Hypothesis` JSON columns and the Phase 3 `Incident.jira_issue_key` column). Idempotent; a `NOT NULL` column with no default on a populated table is skipped, not fatal.

## Integration architecture
- **Prometheus** (`backend/app/integrations/prometheus.py`): `PrometheusClient` — `health()` (`GET /api/v1/status/buildinfo`, never raises), `query()` (PromQL, retries 5xx/timeout twice, fails fast on 4xx/bad payloads), `service_metrics()` (cpu/memory/error-rate/latency/availability fetched concurrently). Label values are PromQL-escaped. NaN/Inf/empty results → `available: false`.
- **Logs** (`backend/app/integrations/logs.py`): provider adapters (`ElasticsearchProvider`, `OpenSearchProvider`, `NoLogProvider`) sharing a `_search` base with retries on 5xx/timeout. `search_logs_real()` returns `{provider, available, entries, groups, error}`; `group_exceptions()` aggregates by exception type; `to_evidence()` converts entries + group summaries to evidence rows. Elasticsearch takes precedence when both are configured.
- **Grafana** (`backend/app/integrations/grafana.py`): pure functions over settings — `dashboard_url(service)` and `grafana_status()`. No HTTP, no secrets.
- **Alertmanager** (`POST /api/webhooks/alert` in `main.py`): payloads with an `alerts` key are validated as `AlertmanagerPayload`; firing alerts become incidents (severity mapped critical→P1, warning→P2, else P3; service from `service`/`job`/`instance` labels), deduped by fingerprint, `resolved` alerts skipped. The legacy `{title, service, …}` shape is handled on the same endpoint. Optional `ALERT_WEBHOOK_SECRET` enforces an `X-Alert-Token` header (constant-time compare).
- **Jira** (`backend/app/integrations/jira.py`, Phase 3): `JiraClient` — `create_issue(incident, hypotheses, evidence_summary)` (`POST /rest/api/3/issue`, ADF description, severity→priority P1 Highest / P2 High / P3 Medium / P4 Low, validates the response carries a `key`), `update_issue_status(issue_key, status)` (GET transitions, POST the matching one; Jira answers the transition POST with 204). Auth is Basic (email + API token) in the header only — the token is never logged or returned. Retries 5xx/timeout twice, fails fast on 4xx/bad payloads. The created key is stored on `Incident.jira_issue_key` (added by the `add_missing_columns` migration path) and surfaced as `jira_issue_key` / `jira_issue_url` in `incident_out`. `jira_status()` reports configured/url/project for the status endpoint (field named `project`, never `project_key`, so the response contains no `key` substring).
- **Slack** (`backend/app/integrations/slack.py`, Phase 3): `SlackClient` — `post_message(text)` (POST to the incoming-webhook URL, success = HTTP 200 with body `ok`) and `notify(event, incident, hypothesis, detail, app_url)` for events `new_incident` / `hypothesis_ready` / `plan_approved` / `plan_executed` / `resolved`, each message carrying severity, service, confidence and the incident id/link. The webhook URL is only ever POSTed to — never logged or returned. Same retry policy as Jira. `slack_status()` reports configured/channel.
- **Notify endpoint** (`POST /api/incidents/{id}/notify` in `main.py`, Phase 3): auth required, admin/responder only, body `{"channel": "jira" | "slack"}` (`NotifyIn`). Jira: idempotent — an already-linked incident returns the existing key (`already_linked: true`) instead of a duplicate issue; otherwise creates the issue from the incident + ranked hypotheses + evidence count and records a `notification` event. Slack: posts `hypothesis_ready` when a hypothesis exists, else `new_incident`. Errors: 404 unknown incident, 503 channel not configured, 502 channel call failed, 422 invalid channel. Best-effort hooks (also Phase 3): `_run_bg` posts a Slack notification when an investigation finishes, and the `verify` endpoint posts one when verification passes — both via `_notify_slack_best_effort`, which swallows all failures and is a no-op when Slack is unconfigured, so notification can never break the incident flow or change webhook/simulate behaviour.
- **Investigation wiring** (`agents.py` `run_investigation`): in live mode the Logs agent queries the configured provider over the last 60 minutes and the Metrics agent queries Prometheus; in demo mode both use synthetic fixtures labelled `demo/synthetic`. A hypothesis requires usable evidence (everything except `integration_unavailable` items) and confidence ≥ 0.65, otherwise the run abstains. Live-mode guard (Phase 8): if Groq is down and the alert matches no known scenario, the run abstains rather than falling back to a deterministic fixture — see README "Known limitations".
- **Status** (`GET /api/integrations/status`): Prometheus configured/reachable (live check, capped at 5 s), logs provider/configured, Grafana configured/url, Jira configured/url/project, Slack configured/channel. Never returns tokens or secrets.

## Local dev
```bash
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && cp .env.example .env
uvicorn app.main:app --reload --port 8000
cd frontend && npm install && npm run dev   # http://localhost:5173
```
Tests: `cd backend && .venv/bin/python -m pytest tests -q` (99 tests, all mocked — no real infra). Eval: `python -m evals.run_eval` (writes `backend/evals/results.json`, demo-labelled unless a Groq key is set).

## Docker
`docker compose up --build` (backend :8000, frontend :5173, Postgres :5432 — the `postgres` service is `pgvector/pgvector:pg16` and the backend's `DATABASE_URL` already points at it). Optional local Prometheus under the `monitoring` profile — see `prometheus.yml` and the README Docker section; the default `up` is unchanged without the profile.

## Deploy (free)
- Backend: Render free web service, `GROQ_API_KEY` + `DATABASE_URL` (Neon free Postgres) as env vars. **Set a strong `JWT_SECRET` too — the Phase 4 build refuses to start in live mode with the demo default.**
- Frontend: Vercel free, `VITE_API_URL` = Render backend URL.
- Local demo: `docker compose up --build`.
- Production checklist (JWT secret, CORS, webhook secret, integration verification, eval re-run): see README.
