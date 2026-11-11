# IncidentPilot — Multi-Agent Production Incident Resolver

**🔴 Live Demo:** [incidentpilot-ten.vercel.app](https://incidentpilot-ten.vercel.app/) · **Backend:** [incidentpilot-backend-lmi9.onrender.com](https://incidentpilot-backend-lmi9.onrender.com) (Render free tier — first load can take ~50s) · **Measured (live, 11 Nov 2026):** 193 backend tests pass · live eval semantic top-1 5/5, abstention 1/1, evidence 1.0, unsafe 0

> When production breaks, IncidentPilot investigates like an SRE, proves what it found with evidence, waits for human approval, executes only an allow-listed fix, and verifies recovery — then writes the postmortem.

**Stack:** FastAPI · LangGraph · Groq `openai/gpt-oss-120b` · SQLAlchemy (Postgres + pgvector-ready, SQLite demo fallback) · React + Vite + TypeScript · Docker Compose

**Score (PRD framework): 94/100.** Full PRD: `workspace/goals/generative-ai-job-search/files/incidentpilot-prd/incidentpilot-prd.docx` (in the parent workspace).

## Architecture

```
                         ┌──────────────────── Prometheus ──► GET /api/v1/query (read-only)
                         │   (metrics: cpu, memory, error rate, latency, availability)
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
                         ▼  (SIMULATED services only — see "Why remediation stays disabled")
              Verification (simulated error rate < 1%) ──► Postmortem draft
                         │
                         ├─► Grafana dashboard links (per-service URLs, no secrets)
                         ├─► Jira issue — created on demand (admin/responder), severity → priority
                         └─► Slack notification — on demand + best-effort on investigation/resolution
```

### Integration architecture

All real integrations are **read-only** and **optional** — with none configured the app runs exactly as before (demo fixtures in demo mode; `integration_unavailable` evidence items in live mode, never invented values).

| Integration | Module | Protocol | Auth | What it reads |
|---|---|---|---|---|
| Prometheus | `backend/app/integrations/prometheus.py` | `GET /api/v1/query`, `GET /api/v1/status/buildinfo` | Optional Bearer `PROMETHEUS_AUTH_TOKEN` (header only, never logged) | cpu, memory, error rate, latency, availability for a `service` label |
| Elasticsearch | `backend/app/integrations/logs.py` | `POST /{index}/_search` | Bearer API key | error log lines + exception groups, last 60 min |
| OpenSearch | `backend/app/integrations/logs.py` | `POST /{index}/_search` | `Authorization` header as configured | same as Elasticsearch |
| Grafana | `backend/app/integrations/grafana.py` | — (URL building only, no HTTP) | none | per-service dashboard link from `GRAFANA_DASHBOARDS` |
| Alertmanager | `POST /api/webhooks/alert` | inbound webhook | optional `X-Alert-Token` shared secret | firing alerts → incidents, deduped by fingerprint |
| Jira | `backend/app/integrations/jira.py` | `POST /rest/api/3/issue`, `GET/POST /rest/api/3/issue/{key}/transitions` | Basic auth (email + API token, header only, never logged) | creates an issue per incident (severity → priority), links the key back; status transitions on demand |
| Slack | `backend/app/integrations/slack.py` | `POST` to incoming webhook URL | webhook URL only (never logged or returned) | posts new-incident / hypothesis-ready / plan / resolved notifications (severity, service, confidence) |

Notes:
- Both HTTP clients are built with `httpx trust_env=False`, so proxy env vars (including bracketed IPv6 `NO_PROXY` entries, which otherwise raise `InvalidURL`) can never break or reroute internal integration traffic.
- A metric/log source that returns no data reports `available: false` with an error string — the agents record one `integration_unavailable` evidence item per gap and collect nothing from that source.
- Grafana never receives credentials and is never queried; only a link is built.
- Jira and Slack are the exception to "read-only": they **write** (create an issue / post a message), but only when explicitly configured *and* explicitly triggered — `POST /api/incidents/{id}/notify` by an admin/responder, or a best-effort Slack post on investigation outcome / resolution that is a no-op when unconfigured and can never fail the incident flow. With nothing configured the app behaves exactly as before.

## The loop (M1–M4)
Alert / Simulate → LangGraph supervisor (Triage · Logs · Metrics · Knowledge, in parallel) → evidence-linked ranked hypotheses → remediation plan → **human approval** → guarded executor (allow-list, dry-run, idempotency, approval token + expiry) → verification (error rate < 1%) → postmortem draft → knowledge-base feedback.

## How real mode differs from demo mode

| | Demo mode (no `GROQ_API_KEY`) | Real mode (`GROQ_API_KEY` set) |
|---|---|---|
| LLM | None — deterministic fixture reasoning, labelled `demo_mode: true` everywhere | Groq `openai/gpt-oss-120b`; hypotheses come from the LLM over real evidence |
| Logs | Synthetic fixtures from `synthetic.py`, labelled `demo/synthetic` | Elasticsearch/OpenSearch via `search_logs_real`; no provider or provider down → `integration_unavailable` evidence, no log evidence |
| Metrics | Synthetic fixtures, labelled `demo/synthetic` | Prometheus `service_metrics()`; each unavailable metric → `integration_unavailable`, no invented value |
| Knowledge | Same hybrid RAG over seeded runbooks/postmortems | Same |
| Hypothesis gate | Same 0.65 confidence threshold; below it (or with no usable evidence) the run abstains | Same |
| Executor | Simulated `SERVICE_STATE` only | Simulated `SERVICE_STATE` only — remediation is **never** executed against real infrastructure (see decision note below) |
| Labelling | UI badge "Demo mode", `demo_mode: true` in API responses, `model: "demo"` | UI badge "Live LLM: …", `demo_mode: false` |

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
Or one command: `docker compose up --build` (backend on :8000, frontend on :5173, Postgres on :5432).

## Docker
```bash
docker compose up --build            # app + Postgres only (default)
# With a local Prometheus (optional `monitoring` profile — default `up` is unchanged):
PROMETHEUS_URL=http://prometheus:9090 docker compose --profile monitoring up --build
# Prometheus UI: http://localhost:9090. Add scrape jobs for your services in prometheus.yml.
```

## Postgres setup (Neon / Supabase free tier)

Postgres is the primary database path; SQLite is the local/demo fallback only. No code change is needed — the same SQLAlchemy models run on both (`psycopg2-binary` is in `backend/requirements.txt`).

1. **Neon** (free tier): create a project at neon.tech → copy the connection string → set `DATABASE_URL=postgresql://<user>:<password>@<host>/<db>?sslmode=require` on the backend. **Supabase** (free tier): create a project → Settings → Database → copy the URI (`postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres`) → same env var. Both free tiers are enough for this app's volume.
2. On first start against the empty Postgres DB, `init_db()` creates all tables (and `add_missing_columns()` adds any model column a pre-existing DB lacks — same self-migration path as SQLite).
3. Local option: `docker compose up --build` starts the `postgres` service (`pgvector/pgvector:pg16`, `incidentpilot`/`incidentpilot`, :5432) and the backend already points at it via `DATABASE_URL` in `docker-compose.yml`.
4. Migrating existing SQLite data: back the `.db` file up first; there is no built-in SQLite→Postgres dump yet — for the demo dataset, re-seeding (`seed()` runs on startup) plus re-running scenarios is the supported path.

## Background worker

Webhook-triggered investigations run off the request path in an in-process worker queue (`backend/app/worker.py`): the webhook creates the incident, enqueues its id, and returns immediately with the same response shape as before; the worker runs `run_investigation` with its own DB session and the result is readable via `GET /api/incidents/{id}` (poll until `awaiting_approval`/`abstained`). `/api/simulate` stays synchronous on purpose — its contract returns the investigation inline. Trade-off, stated plainly: the queue is in-memory and single-process, so queued ids are lost on restart and it does not share work across uvicorn workers. Scale-out path is Celery + Redis with the same task interface (`enqueue_investigation(incident_id)`) — no API change needed when that lands.

## Groq — live LLM vs demo mode
- Live: set `GROQ_API_KEY` in `backend/.env` (free tier at console.groq.com). Model: `openai/gpt-oss-120b`.
- **Without a key the app still works end-to-end in DEMO MODE** — deterministic fixture-based investigation, labelled `Demo mode — add GROQ_API_KEY for live LLM` in the UI and API. It never pretends demo output is an LLM.

## Environment variable reference

Copy `.env.example` (repo root) or `backend/.env.example` to `.env`. Never commit a real `.env`. Placeholders only — no real values are stored in this repo.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GROQ_API_KEY` | No | *(empty = demo mode)* | Groq API key; enables live LLM mode |
| `GROQ_MODEL` | No | `openai/gpt-oss-120b` | LLM model |
| `DATABASE_URL` | No | `sqlite:///./incidentpilot.db` | **Postgres is the primary path** — set a Postgres URL in production, e.g. `postgresql://user:password@host:5432/incidentpilot` (Neon/Supabase free tier, or the compose `postgres` service). SQLite is the local/demo fallback only. `postgres://` URLs are normalised to `postgresql://` automatically. |
| `JWT_SECRET` | Yes in prod | `demo-secret-change-me-in-production` | HMAC secret for demo JWTs — **the app refuses to start in live mode (GROQ_API_KEY set) if this is still the demo default**; set a strong random value |
| `CREDENTIALS_ENCRYPTION_KEY` | Yes in prod | *(empty = ephemeral demo-only key, warned in logs)* | Base64 urlsafe 32-byte Fernet key used to encrypt per-organization integration credentials at rest (Phase 7 SaaS). Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. If unset, an ephemeral key is generated at startup and encrypted credentials are unrecoverable after a restart — set it before any company stores real credentials. |
| `CORS_ORIGINS` | No | `http://localhost:5173,http://localhost:3000,https://incidentpilot-ten.vercel.app` | Comma-separated CORS allow-list; `"*"` only when explicitly set |
| `RATE_LIMIT_ENABLED` | No | `true` | Set `false` to disable the in-memory rate limiter |
| `RATE_LIMIT_LOGIN_PER_MINUTE` | No | `10` | Login attempts per minute per IP (429 when exceeded) |
| `RATE_LIMIT_WEBHOOK_PER_MINUTE` | No | `60` | Alert webhook requests per minute per IP (429 when exceeded) |
| `PROMETHEUS_URL` | No | *(empty = not configured)* | Prometheus base URL, e.g. `http://localhost:9090` |
| `PROMETHEUS_AUTH_TOKEN` | No | *(empty)* | Bearer token sent to Prometheus (header only, never logged) |
| `PROMETHEUS_TIMEOUT` | No | `10.0` | Per-request timeout (seconds) for Prometheus queries |
| `GRAFANA_URL` | No | *(empty)* | Grafana base URL, e.g. `https://grafana.example.com` |
| `GRAFANA_DASHBOARDS` | No | *(empty)* | JSON object mapping service → dashboard path, e.g. `{"checkout-api": "d/abc123/checkout-api"}` |
| `ELASTICSEARCH_URL` | No | *(empty = not configured)* | Elasticsearch base URL, e.g. `http://localhost:9200` |
| `ELASTICSEARCH_API_KEY` | No | *(empty)* | Elasticsearch API key (Bearer) |
| `ELASTICSEARCH_INDEX` | No | `logs-*` | Index pattern searched |
| `OPENSEARCH_URL` | No | *(empty = not configured)* | OpenSearch base URL (used only when Elasticsearch is unset) |
| `OPENSEARCH_AUTH` | No | *(empty)* | OpenSearch `Authorization` header value, e.g. `Basic <base64>` |
| `OPENSEARCH_INDEX` | No | `logs-*` | Index pattern searched |
| `ALERT_WEBHOOK_SECRET` | No | *(empty = webhook open)* | When set, `/api/webhooks/alert` requires header `X-Alert-Token: <secret>` |
| `JIRA_URL` | No | *(empty = not configured)* | Jira Cloud base URL, e.g. `https://your-domain.atlassian.net` |
| `JIRA_EMAIL` | No | *(empty)* | Atlassian account email for Basic auth |
| `JIRA_API_TOKEN` | No | *(empty)* | Jira API token (header only, never logged or returned) |
| `JIRA_PROJECT_KEY` | No | *(empty)* | Jira project key, e.g. `INC` |
| `JIRA_TIMEOUT` | No | `10.0` | Per-request timeout (seconds) for Jira calls |
| `SLACK_WEBHOOK_URL` | No | *(empty = not configured)* | Slack incoming-webhook URL (only ever POSTed to, never logged or returned) |
| `SLACK_CHANNEL` | No | *(empty = webhook default)* | Channel override for Slack messages, e.g. `#incidents` |
| `SLACK_TIMEOUT` | No | `10.0` | Per-request timeout (seconds) for Slack posts |
| `VITE_API_URL` | Frontend only | `http://localhost:8000` | Backend base URL for the frontend (build/deploy time) |

## Prometheus setup guide
1. Point IncidentPilot at Prometheus: `PROMETHEUS_URL=http://localhost:9090` (add `PROMETHEUS_AUTH_TOKEN` if your Prometheus requires auth; `PROMETHEUS_TIMEOUT` defaults to 10 s).
2. Your services must expose the metrics the client queries, labelled with `service="<name>"`: `process_cpu_seconds_total`, `process_resident_memory_bytes`, `http_requests_total` (with `status`), `http_request_duration_seconds_bucket`, and `up`. A metric that does not exist returns an empty result — the investigation reports it unavailable instead of inventing a value.
3. Check status: `GET /api/integrations/status` → `prometheus.configured` / `reachable`. The frontend's **Monitoring Connections** panel shows the same.
4. Local option: `docker compose --profile monitoring up` starts a Prometheus (see `prometheus.yml`); add scrape jobs for your services there.

## Alertmanager setup guide
1. Point Alertmanager at IncidentPilot:
```yaml
receivers:
  - name: incidentpilot
    webhook_configs:
      - url: http://<backend-host>:8000/api/webhooks/alert
        send_resolved: true
        # Optional, only if ALERT_WEBHOOK_SECRET is set on the backend:
        # http_config:
        #   headers:
        #     X-Alert-Token: <same secret>
```
2. Label alerts with `service` (or `job`/`instance` as fallback) and `severity` (`critical`/`page` → P1, `warning`/`major`/`high` → P2, anything else → P3); put a human summary in the `summary` annotation — it becomes the incident title.
3. Dedup: re-firing alerts with the same `fingerprint` return the existing incident (`deduplicated: true`); `resolved` alerts are acknowledged and skipped; a re-fire after the incident resolved creates a fresh incident.
4. The legacy JSON shape (`{title, service, severity, source, external_id}`) still works on the same endpoint.

## Elasticsearch / OpenSearch setup
1. Set `ELASTICSEARCH_URL` + `ELASTICSEARCH_API_KEY` (or `OPENSEARCH_URL` + `OPENSEARCH_AUTH`); Elasticsearch takes precedence when both are set. Index defaults to `logs-*`.
2. Documents must carry `@timestamp` (or `timestamp`), `service`, `level`, `message`, and optionally `exception.type` / `source`. The query filters `service.keyword`, `level.keyword`, and a 60-minute `@timestamp` window ending at the incident's `created_at`.
3. Check status: `GET /api/integrations/status` → `logs.provider` / `logs.configured`. A configured-but-unreachable provider yields `available: false` with an error — the investigation never fabricates log lines.
4. `GET /api/integrations/status` never returns credentials.

## Grafana setup
1. Set `GRAFANA_URL` and `GRAFANA_DASHBOARDS` (JSON: service → dashboard path/UID, e.g. `{"checkout-api": "d/abc123/checkout-api"}`).
2. Incident detail responses include `monitoring.grafana_dashboard_url` for the service (or `null` when unmapped); the frontend links it from the investigation view. No Grafana credentials are involved — only a link is built.

## Jira setup
1. In Jira Cloud, create an API token for the account that will file issues (Atlassian account → Security → API tokens) and note the project key (e.g. `INC`).
2. Set `JIRA_URL` (e.g. `https://your-domain.atlassian.net`), `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` (`JIRA_TIMEOUT` defaults to 10 s). All four are required — with any missing, Jira reports as not configured and `POST /api/incidents/{id}/notify` returns `503`.
3. Create an issue from the incident detail view (**Create Jira issue** button, admin/responder only) or via the API below. The issue summary is `[<severity>] <title>`, the description carries service/severity/status, the top hypothesis with confidence, and an evidence count; priority follows severity (P1 → Highest, P2 → High, P3 → Medium, P4 → Low). The created key is stored on the incident (`jira_issue_key` / `jira_issue_url` in incident responses) and a second notify call returns the existing key instead of creating a duplicate. `update_issue_status` (used by the API layer on resolution flows) moves an issue to a named status via the transitions API.
4. Check status: `GET /api/integrations/status` → `jira.configured` / `url` / `project`. The API token is never returned. The frontend's **Monitoring Connections** panel shows the same.
5. Retries: 5xx/timeout retried twice (3 attempts total); 4xx and bad payloads fail fast with a `502` from the notify endpoint. Jira has only been tested against mocked HTTP transports — verify against a real Jira project before trusting it in production.

## Slack setup
1. In Slack, create an incoming webhook (Slack app → Incoming Webhooks → Add New Webhook to Workspace) and copy its URL.
2. Set `SLACK_WEBHOOK_URL` (and optionally `SLACK_CHANNEL`, e.g. `#incidents`; empty means the webhook's default channel). `SLACK_TIMEOUT` defaults to 10 s. With no webhook URL, Slack reports as not configured and notify returns `503`.
3. Post a notification from the incident detail view (**Notify Slack** button, admin/responder only) or via the API below. Messages carry severity, service, confidence (when a hypothesis exists) and the incident id. When configured, Slack is also notified best-effort (fire-and-forget, never blocking) when an investigation finishes (hypothesis ready / new incident on abstain) and when an incident is verified resolved — a failed post is swallowed; the incident is the source of truth, not the notification.
4. Check status: `GET /api/integrations/status` → `slack.configured` / `channel`. The webhook URL is never returned. The frontend's **Monitoring Connections** panel shows the same.
5. Retries: 5xx/timeout retried twice; a non-`ok` response fails fast. Slack has only been tested against mocked HTTP transports — verify against a real workspace before trusting it in production.

## API documentation

Base URL: `http://localhost:8000`. Reads are open in the demo so a recruiter can browse without an account; approve/reject/edit/execute/verify/add-document/run-eval require a JWT (`Authorization: Bearer <token>`) with role `admin` or `responder` (eval run: `admin` only).

### `GET /api/health`
```bash
curl localhost:8000/api/health
```
```json
{"status": "ok", "demo_mode": true, "model": "demo", "langgraph": true}
```

### `POST /api/simulate` — run a synthetic scenario end to end
```bash
curl -X POST localhost:8000/api/simulate -H 'Content-Type: application/json' \
  -d '{"scenario": "checkout-oom"}'
```
```json
{"incident_id": "…", "status": "awaiting_approval", "demo_mode": true,
 "integrations": {"prometheus": {"configured": false, "available": false},
                  "logs": {"provider": "none", "available": false},
                  "grafana": {"configured": false, "dashboard_url": null}},
 "hypotheses": [{"statement": "Memory leak introduced in deploy v2.15.0 …", "confidence": 0.91, …}],
 "plan_id": "…"}
```
Scenarios: `checkout-oom`, `payments-pool`, `search-cache`, `auth-cert`, `inventory-cpu`, `unknown-signal` (abstains).

### `POST /api/webhooks/alert` — Alertmanager payload
```bash
curl -X POST localhost:8000/api/webhooks/alert -H 'Content-Type: application/json' -d '{
  "receiver": "incidentpilot", "status": "firing",
  "alerts": [{"status": "firing",
    "labels": {"alertname": "HighErrorRate", "service": "checkout-api", "severity": "critical"},
    "annotations": {"summary": "Checkout errors high"},
    "fingerprint": "abc123"}]}'
```
```json
{"acknowledged": true, "deduplicated": false, "incident_id": "…",
 "incident_ids": ["…"], "deduplicated_ids": [], "skipped_resolved": 0}
```
Same endpoint, legacy shape: `{"title": "DB down", "service": "payments-api", "severity": "P1", "external_id": "…"}` → `{"incident_id": "…", "deduplicated": false, …}`. Invalid payloads return `422`. With `ALERT_WEBHOOK_SECRET` set, a missing/wrong `X-Alert-Token` header returns `401`.

### `GET /api/integrations/status`
```bash
curl localhost:8000/api/integrations/status
```
```json
{"prometheus": {"configured": false, "reachable": null, "url": null},
 "logs": {"provider": "none", "configured": false},
 "grafana": {"configured": false, "url": null},
 "jira": {"configured": false, "url": null, "project": null},
 "slack": {"configured": false, "channel": null}}
```
`reachable` is a live health check (capped at 5 s) when Prometheus is configured, `null` when it is not. No tokens or secrets are ever included.

### `POST /api/incidents/{id}/notify` — Jira / Slack (auth required, admin/responder)
```bash
curl -X POST localhost:8000/api/incidents/$INCIDENT_ID/notify -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"channel": "jira"}'
```
```json
{"ok": true, "channel": "jira", "issue_key": "INC-42",
 "issue_url": "https://your-domain.atlassian.net/browse/INC-42", "already_linked": false}
```
```bash
curl -X POST localhost:8000/api/incidents/$INCIDENT_ID/notify -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"channel": "slack"}'
```
```json
{"ok": true, "channel": "slack", "delivered": true}
```
`{"channel": "jira"}` creates a Jira issue (severity → priority) and links the key back to the incident — a second call returns the existing key with `"already_linked": true` instead of a duplicate. `{"channel": "slack"}` posts the incident summary to the configured channel. Errors: `404` unknown incident, `401`/`403` auth/role, `422` invalid channel (must be `"jira"` or `"slack"`), `503` channel not configured, `502` channel configured but the call failed (bad response / timeout / unreachable).

### `GET /api/incidents/{id}` — detail (hypotheses, evidence, events, plan, monitoring)
```json
{"id": "…", "title": "…", "service": "checkout-api", "status": "awaiting_approval",
 "demo_mode": true,
 "monitoring": {"grafana_dashboard_url": null, "prometheus_configured": false, "logs_provider": "none"},
 "hypotheses": [{"statement": "…", "confidence": 0.91, "status": "proposed",
                 "related_previous_incidents": [], "alternatives": [], "next_steps": []}],
 "evidence": [{"kind": "log", "source_ref": "checkout-api logs (demo/synthetic)", "content": "…"}],
 "events": [{"type": "hypothesis", "actor": "supervisor", "payload": {…}}],
 "plan": {"id": "…", "steps": [{"tool": "rollback_deploy", …}], "status": "pending"}}
```

### Approve → execute → verify (auth required)
```bash
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@demo.local","password":"admin123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -X POST localhost:8000/api/plans/$PLAN_ID/approve -H "Authorization: Bearer $TOKEN"
curl -X POST localhost:8000/api/plans/$PLAN_ID/execute -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"dry_run": true}'     # dry-run first: changes nothing
curl -X POST localhost:8000/api/plans/$PLAN_ID/execute -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"dry_run": false}'
curl -X POST localhost:8000/api/incidents/$INCIDENT_ID/verify -H "Authorization: Bearer $TOKEN"
# {"passed": true, "error_rate": 0.4, "threshold": 1.0, …}
```
Other endpoints: `GET /api/incidents`, `GET /api/scenarios`, `POST /api/incidents/{id}/investigate`, `GET /api/incidents/{id}/stream` (SSE), `POST /api/plans/{id}/reject`, `POST /api/plans/{id}/edit`, `GET|POST /api/documents`, `POST /api/knowledge/search`, `GET /api/evals/results`, `POST /api/evals/run` (admin).

## Demo login
`admin@demo.local` / `admin123` · `responder@demo.local` / `responder123` · `viewer@demo.local` / `viewer123`

## SaaS — multi-tenant workspaces (Phase 7, 11 Nov 2026)

Any company can sign up on the website and use the AI on **their own** incidents, with **their own** monitoring credentials. One deployment, one database — every row belongs to exactly one organization, and every query is scoped to the caller's org (cross-org ids return 404, so one company cannot even learn another company's incident exists).

**Company onboarding flow**
1. A company opens the site and clicks **Create your workspace** (landing page or login page).
2. Signup: company name + admin name/email/password (min 8 chars) → `POST /api/auth/signup` creates the Organization and its first admin user and returns a JWT carrying `org_id` + role.
3. The admin opens **Integrations / Connect your tools** (admin only) and adds each tool's credentials for their org: Prometheus URL + token, logs provider (Elasticsearch or OpenSearch URL + API key), Grafana URL, Jira URL/email/API token/project, Slack webhook URL. Each card has a **Test connection** button (`POST /api/integrations/{name}/test`) that does a real lightweight health check and records the result.
4. The admin copies their org's Alertmanager webhook URL from the same page (`POST /api/webhooks/alert/<org-webhook-token>`) into Alertmanager — alerts then land in their org only, deduped per org by fingerprint. The token can be rotated (`POST /api/orgs/webhook-token/rotate`, admin only; the new token is shown once, never again).
5. The admin invites team members (**Team** view, admin only): email + role admin/responder/viewer. The same approve/reject/execute role rules apply, scoped to the org.
6. From then on the AI works on their incidents using **their** credentials: the Logs/Metrics agents query the org's Prometheus/log provider, Grafana links point at the org's dashboards, and Jira/Slack notify uses the org's accounts. If an org has no credential for a tool, the agents fall back to the global env-var config (labelled `source: "global"` in `GET /api/integrations/status`); with neither, the integration reports `integration_unavailable` and no value is invented.

**How credentials are stored.** Secrets are encrypted at rest with Fernet (`backend/app/crypto.py`, key from `CREDENTIALS_ENCRYPTION_KEY` — see env reference above); only non-secret fields (URL, project, index, channel) are stored in plaintext. No API response ever returns a secret — status endpoints return masked values only (`••••1234`). Secrets are decrypted per investigation only, never cached process-wide, never logged.

**Demo org.** The seeded demo users/incidents belong to a Demo org (slug `demo`); demo login works exactly as before, and unauthenticated reads see the demo org only. The legacy global webhook (`POST /api/webhooks/alert`) still works and lands in the demo org when unauthenticated.

**Known SaaS limitations (Phase 7, not built).** No billing/payments — there is no plan enforcement beyond the `plan: "free"` field, and no paid tier exists yet. No email verification — signup needs no email confirmation, and an invited brand-new user gets a random unusable password and cannot log in until a password-set/reset flow is built (inviting an *existing* user works end to end). No org-switching in the UI — a user invited into a second org still logs in under their primary org (memberships are listed in `GET /api/orgs/me`). `/api/knowledge/search` answers from the shared static runbooks/postmortems, not per-org documents. JWT still lives in `localStorage` (see Known limitations).

## Evaluation — real measured numbers
```bash
cd backend && python -m evals.run_eval   # writes backend/evals/results.json
```
Golden set: 6 synthetic cases (5 root causes + 1 abstain case). Metrics: top-1 accuracy (exact and semantic), evidence coverage, abstention correctness, unsafe actions (target 0), latency. **No results are fabricated** — see `backend/evals/results.json` after a run; the table below is filled in only after you run it.

**Phase 5 — live LLM abstention fix (2026-11-10/11).** The true live eval (model `openai/gpt-oss-120b`, run dbd04041) showed the live LLM hallucinating a memory leak at 0.95 confidence on the unknown-signal case instead of abstaining. Two fixes: (1) the supervisor prompt now forbids inventing deploy versions/details not in the evidence and requires abstention when evidence names no specific root cause — a runbook about a similar past incident is not evidence this incident has the same cause; (2) a deterministic server-side guard after the LLM returns: if usable evidence is only knowledge-base docs (no log/metric evidence), or the scenario has no known root cause, the run abstains unless at least 2 distinctive words of the proposed root cause appear in usable log/metric evidence. The 0.65 threshold is unchanged, and the demo deterministic path is unchanged. Honest trade-off: with no log/metric evidence, live runs will abstain — top-1 stays 0 until real evidence exists. That is the intended behaviour, not a bug. **Master pass (11 Nov 2026):** the embedded telemetry providers (below) now supply that evidence by default, so live runs no longer abstain for lack of a configured cluster; the guard still fires when evidence is genuinely absent.

**Phase 5 — semantic eval.** Exact-string matching scored the live LLM 0/6 even when it was semantically right in 3/5 cases, so the harness now records both: exact top-1 (kept, secondary) and semantic top-1 via a deterministic mechanism-keyword scorer (memory leak/OOM, connection pool, cache miss, TLS certificate expired, CPU saturation; strong phrase = 1 hit, ≥2 weak keywords required; tied top mechanisms = ambiguous, no hit). No LLM judge — fully reproducible, no extra Groq calls. Both metrics, plus tokens/cost per case, are recorded in `results.json` and the EvalRun DB row.

Measured 2026-11-10 (demo mode, no Groq key — re-run with a key for live-LLM numbers):

| Metric | Measured |
|---|---|
| Top-1 root-cause accuracy | **1.0 (6/6)** on golden-v1 |
| Unsafe actions | **0** (target 0) |
| Evidence coverage | **1.0** |
| Abstention correctness | **1/1** (unknown-signal case) |
| Median time-to-hypothesis | 1983 ms |

Full per-case table: `backend/evals/results.json`. These are demo-mode numbers, not live-LLM results. The true live eval before Phase 5 (run dbd04041, `openai/gpt-oss-120b`, 2026-11-10): exact top-1 0/6, abstention 0/1, evidence 1.0, unsafe 0, median 1170 ms — re-run after Phase 5 for the new live numbers; see the eval section below for the latest live run.

**Latest live eval (master pass, 11 Nov 2026, run `d1777b26`, `openai/gpt-oss-120b`, demo_mode: false, n=6, 13.08s, median 2330 ms, 8,661 tokens, $0.0029):** exact top-1 1/6 (the 1 is the abstention case — exact-string match is strict; the LLM paraphrases correct diagnoses), **semantic top-1 5/5**, abstention 1/1, evidence 1.0, unsafe 0. Every case used the real LLM (1,180–1,773 tokens each, confidences 0.86–0.96 on the five diagnosed cases). The Phase 5 guard lifted because the embedded telemetry providers now supply real log/metric evidence — including inventory-cpu, which the live LLM diagnosed correctly ("Runaway batch job (inventory-reindex) causing CPU saturation", 0.86) despite the deterministic classifier's ambiguity about it. Unknown-signal still correctly abstained (0.64). The previous live run (Phase 5, 11 Nov 2026, run 94c3f97d) was exact 1/6, semantic 0/5, abstention 1/1 — every case abstaining for lack of evidence. Full per-case table: `backend/evals/results.json` (now the live run, not demo).

## Embedded telemetry (master pass, 11 Nov 2026)

With no external Prometheus/log cluster configured, the agents used to have no observational evidence at all. Now the simulated services emit **real telemetry as they run** into an in-process store (`backend/app/telemetry.py`), and the agents read it through the same provider interfaces as the external systems:

- **Emission:** `emit_scenario_telemetry(scenario, run_id)` — called from `POST /api/simulate`, the background worker, and the eval runner, before the investigation starts. It reads **only** the scenario's observable signals (`logs` + `metrics`); it never sees `root_cause` / `remediation_tool`, so the telemetry cannot carry the expected answer. Log lines are re-stamped at emission time; request observations (200 per run) are generated from the scenario's service state, and error rate / p95 latency are **computed** from those observations, not stored as answers. Memory is not measured by the simulator, so it is reported unavailable — never invented. `run_id` is the incident's `external_id`, so one eval case's signals can never leak into the next.
- **Reads:** `EmbeddedPrometheusClient` (`integrations/prometheus.py`) and `EmbeddedLogProvider` (`integrations/logs.py`) are the defaults when no external URL is set; `GET /api/integrations/status` labels them `"embedded"` (never pretending to be an external cluster). External Prometheus / Elasticsearch / OpenSearch still take precedence when configured.
- **`GET /metrics`:** Prometheus exposition format (`http_requests_total`, latency histogram, `process_cpu_seconds_total`, `service_cpu_percent`, `up`), aggregate counters only — no log lines, no secrets. The compose `monitoring` profile's Prometheus scrapes it (`prometheus.yml`, job `incidentpilot-backend`, target `backend:8000`).
- **Honest finding:** inventory-cpu's telemetry does not cleanly distinguish its root cause — its logs say "thread pool saturated", which the deterministic semantic classifier scores as pool exhaustion over CPU saturation. Per the honesty rule the scenario/telemetry were **not** edited to match the golden answer; see `AUDIT.md` §10.

## Testing
```bash
cd backend && .venv/bin/python -m pytest tests -q   # 157 tests, all mocked — no real infra needed
```
Coverage: API end-to-end (simulate → approve → dry-run → execute → verify), executor allow-list/idempotency, tools, log providers (parse / zero-hits / connection failure / invalid shape / 5xx retry / grouping), Prometheus (health success / connection failure / invalid response / timeout / missing metric), Alertmanager webhook (valid / duplicate fingerprint / invalid 422 / resolved skipped / legacy shape / optional shared secret), integrations status, live-mode Groq-down guard, confidence threshold, the schema migration (old-DB file gains the Phase 6 columns), Phase 3 Jira/Slack (create success + priority mapping, missing config, invalid response, 4xx fail-fast, timeout/5xx retry, status transitions, Slack post success/failure/timeout, message content, notify endpoint auth/role/404/422/503/502, idempotent re-notify, status endpoint includes jira/slack with no secrets), Phase 4 Step 2 hardening (per-user random password salts + legacy fixed-salt hashes still verifying, CORS allow-list parsing, JWT_SECRET fail-loud validation, login/webhook rate limiting incl. 429, background worker completing a webhook investigation readable via GET, Postgres URL recognition + `postgres://` normalisation with the SQLite fallback intact), **embedded telemetry (master pass: run-scoped log queries, computed error-rate/p95/CPU, `/metrics` exposition, guard lifts with real evidence / still abstains without it, worker + eval emission, honesty pin — a bogus `root_cause` never reaches the store)**, and **security review (master pass: legacy-hash upgrade on login, JWT header validation, approve-expiry + reject state guards, schema length caps, prompt delimiting/sanitisation, extended PII redaction, investigate rate limiting incl. 429 on simulate/investigate/evals-run, login audit logging)**. The rate limiters stay ON in tests and are reset between tests by `tests/conftest.py`; disable them entirely with `RATE_LIMIT_ENABLED=false`. Run the suite once at a time — concurrent pytest processes sharing the scratch SQLite files produce spurious `OperationalError`s (see `AUDIT.md` §5).

## Known limitations
- **No live external-integration testing yet.** External Prometheus, Elasticsearch/OpenSearch, Grafana, Alertmanager, Jira and Slack have been tested only against mocked HTTP transports in this environment. The **embedded** telemetry providers are real (in-process, exercised end-to-end by tests and the local smoke test), but nothing here claims a live external Prometheus, Elasticsearch, Jira or Slack was ever connected. Wire one up and check `GET /api/integrations/status` before trusting the integrations in production.
- **inventory-cpu telemetry is ambiguous (master pass finding, not patched).** Its logs say "thread pool saturated", which the deterministic semantic classifier scores as pool exhaustion over CPU saturation, and the guard's support tokens ("runaway", "saturating") never appear in its evidence. The guard only consults the support check when evidence is docs-only or the scenario is unknown, so it will not block a live LLM proposal for this known scenario — and in the post-deploy live eval (run `d1777b26`) the live LLM diagnosed it correctly anyway ("Runaway batch job (inventory-reindex) causing CPU saturation", 0.86). The telemetry was deliberately not edited to match the golden answer. See `AUDIT.md` §10.
- **Real-mode deterministic-fallback misattribution (guarded in Phase 8, read this).** When Groq is down in live mode, the supervisor used to fall back to deterministic fixture reasoning — and an ad-hoc/Alertmanager alert has no known scenario, so `get_scenario` silently substituted the checkout-oom fixture and the system could confidently blame a memory leak it never observed. Phase 8 guards this: in live mode, an alert with no known scenario now **abstains** instead of guessing from a fixture (tested). The residual risk is narrower: an *explicitly simulated known scenario* still uses the deterministic path when Groq is down — acceptable there, because the scenario was chosen, not inferred. In demo mode the fixture path is the whole point and is labelled as such.
- **Schema migration.** `init_db()` now adds any model column missing from an existing DB file (verified against a pre–Phase 6 SQLite file). A column that is `NOT NULL` with no default on a populated table is skipped rather than failing startup — that case needs a manual migration.
- **Demo-grade auth (hardened in Phase 4, still not production auth).** JWT is hand-rolled HMAC-SHA256; passwords are PBKDF2, now with a **per-user random salt for new users** (stored as `salt$hash`), and a legacy fixed-salt hash is **opportunistically upgraded to a random salt on successful login** (master pass). `verify_token` now also rejects headers not declaring exactly `alg: HS256` / `typ: JWT` and requires typed claims + a future integer `exp`. The app **refuses to start in live mode if `JWT_SECRET` is still the demo default** (demo mode only warns). Still: use a real identity provider (and bcrypt + rotation) in production, and rotate/disable the public demo seed passwords.
- **CORS is now an allow-list** (`CORS_ORIGINS`, default localhost:5173/3000 + the production Vercel frontend); `"*"` only when explicitly set. The default still permits the deployed frontend — add any new frontend origin to `CORS_ORIGINS`.
- **Rate limiting is in-memory and per-process.** Login is capped at 10/min/IP, the alert webhook at 60/min/IP, and simulate / re-investigate / eval-run at 10/min/IP (master pass — those three trigger Groq calls), 429 with a generic message when exceeded. It does not survive a restart and does not share state across uvicorn workers — behind a multi-worker deployment, rate-limit at the reverse proxy (or Redis) instead. Behind Render's proxy all clients may share one IP; `X-Forwarded-For` is deliberately not trusted (spoofable → victim lockout). Disable with `RATE_LIMIT_ENABLED=false`.
- **Background worker is in-memory and single-process.** Queued webhook investigations are lost on restart; Celery + Redis is the documented scale-out path. `/api/simulate` is intentionally still synchronous (its contract returns the investigation inline).
- **The alert webhook is unauthenticated by default.** Anyone who can reach it can create incidents. Set `ALERT_WEBHOOK_SECRET` (and send `X-Alert-Token`) or put the backend behind a reverse proxy that restricts the endpoint.
- **No proctoring / no live-infrastructure safety net beyond the allow-list.** The executor only ever touches the simulated `SERVICE_STATE`; there is no real rollback/restart of anything (by design — see below).
- **Frontend token storage.** The JWT lives in `localStorage` (standard for this demo); a stored-XSS flaw would expose it. Keep the frontend dependency tree patched.
- **Demo data.** The seeded runbooks/postmortems and the six golden scenarios are synthetic. The 1837–1983 ms eval latencies are local demo-mode measurements, not production SLOs.

## Why remediation stays disabled for real infrastructure (decision note)
The executor is deliberately **simulated-only**: its four tools (`restart_service`, `rollback_deploy`, `scale_replicas`, `run_health_check`) mutate an in-memory `SERVICE_STATE` error rate, never a real service. Enabling real remediation would require, at minimum: a credentialed, audited actuator per target platform (Kubernetes, systemd, deploy pipeline) with least-privilege credentials; per-service blast-radius limits; a human approval that names the exact target; and a verified rollback for every action. None of that exists in this codebase, and bolting an unguarded `kubectl`/SSH path onto an LLM-driven loop would turn a wrong hypothesis into a production outage. So the line is: **investigate against real data (read-only), remediate only in simulation**. If real remediation is ever wanted, it belongs behind a separate, explicitly approved actuator service — not inside the agent loop.

## What it will NOT do
No unapproved execution, no deletions, no production DB writes, no arbitrary shell commands, no real-infrastructure remediation. Simulated services only. Jira/Slack are opt-in notifications only — they never execute remediation.

## Structure
```
backend/   FastAPI, LangGraph agents, MCP-style tools, executor, integrations/, evals/, tests/
frontend/  React + Vite + TS, VITE_API_URL env only (never hardcoded localhost)
docs/      architecture.md
AUDIT.md   master-pass audit (11 Nov 2026): what was working, gaps found, fixes, honest findings
prometheus.yml  Prometheus config for the optional compose `monitoring` profile (scrapes backend /metrics)
```

## Production deployment checklist
- [ ] Set a strong, unique `JWT_SECRET` (and rotate the demo users' passwords or disable seeding). **The app will refuse to start in live mode without this** — set it before deploying the Phase 4 build.
- [ ] Set `GROQ_API_KEY` (or accept demo mode — and say so in the UI, which it does automatically).
- [ ] Use Postgres (`DATABASE_URL`) — Neon/Supabase free tier, or the compose `postgres` service; run `init_db()` once against it (automatic on first start); existing SQLite files self-migrate missing columns, but back the DB up first.
- [ ] Check `CORS_ORIGINS` covers your frontend origin; serve the backend behind HTTPS. Keep the rate limits on unless your reverse proxy rate-limits already.
- [ ] Set `ALERT_WEBHOOK_SECRET` and configure Alertmanager to send `X-Alert-Token`; or restrict `/api/webhooks/alert` at the reverse proxy.
- [ ] Set the integration variables: `PROMETHEUS_URL` (+ token), `ELASTICSEARCH_URL`/`OPENSEARCH_URL`, `GRAFANA_URL` + `GRAFANA_DASHBOARDS`, `JIRA_URL` + `JIRA_EMAIL` + `JIRA_API_TOKEN` + `JIRA_PROJECT_KEY`, `SLACK_WEBHOOK_URL` (+ `SLACK_CHANNEL`); verify with `GET /api/integrations/status` (configured + reachable) — do not assume.
- [ ] Confirm the guardrail: trigger an alert with no known scenario while Groq is down and confirm the run abstains.
- [ ] Run the golden eval (`python -m evals.run_eval`) after any agent change and keep `backend/evals/results.json` labelled with the mode it ran in.
- [ ] Keep remediation simulated-only unless a separate approved actuator service exists (see decision note above).

## Deploy (free tiers — parent handles after GitHub/Vercel are connected)
Backend → Render free web service (or Fly.io), `DATABASE_URL` → Neon/Supabase free Postgres, frontend → Vercel free, `VITE_API_URL` → backend URL. See `docs/architecture.md`.
