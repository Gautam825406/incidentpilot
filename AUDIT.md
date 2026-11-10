# IncidentPilot — Master Pass Audit (11 Nov 2026)

Full-repo audit performed as Phase 1 of the master pass (complete all
remaining work, test, optimize, document, push). Head at audit time:
`99b0121` (Phase 5 complete). Verified by direct inspection of the code
plus the Phase 6 / security / frontend workstreams run under this pass.

Status labels: COMPLETED / PARTIALLY COMPLETED / NOT IMPLEMENTED / BLOCKED.

## 1. Fully implemented (COMPLETED)

- **Core loop**: simulate alert → LangGraph supervisor (Triage, Logs,
  Metrics, Knowledge in parallel) → evidence-linked ranked hypotheses →
  remediation plan → human approval → guarded executor → verification →
  postmortem draft → knowledge-base feedback. Verified by 118 pre-pass
  tests + live smoke tests.
- **Guarded executor**: allow-list exactly
  `restart_service`, `rollback_deploy`, `scale_replicas`,
  `run_health_check`; dry-run separate from live; idempotency keys
  `(plan_id, step, dry/live)`; approval token + 30-min expiry; blocked
  tools recorded as audit events.
- **Human approval**: approve/reject/edit/execute/verify all gated to
  admin/responder; `POST /api/evals/run` admin-only; approval events in
  the incident timeline.
- **LangGraph supervisor**: parallel specialist agents, MemorySaver
  checkpointer, resumable investigations, background worker queue
  (Phase 4) with `/api/simulate` deliberately synchronous.
- **RAG / knowledge**: hybrid retrieval (0.6 vector + 0.4 keyword),
  hash embeddings, document CRUD, knowledge search endpoint.
- **Abstention (Phase 5)**: hardened supervisor prompt + deterministic
  server-side guard (live mode only; docs-only evidence or unknown
  scenario → confidence capped 0.64, abstain, reason
  `phase5_abstention_guard`); explicit LLM abstention respected (5b);
  semantic eval (deterministic keyword/mechanism scorer) with exact kept
  as secondary. 0.65 threshold intact.
- **Integrations (code + mocked tests)**: Prometheus client, Alertmanager
  webhook (fingerprint dedup), Elasticsearch/OpenSearch log providers,
  Grafana dashboard links, Jira (idempotent create), Slack (incoming
  webhook, 5 events). `GET /api/integrations/status` reports all five.
- **Production hardening (Phase 4)**: Postgres primary + SQLite demo
  fallback, per-user random password salts, CORS env allow-list, rate
  limiting, fail-loud JWT check at startup, background worker.
- **Live LLM**: Groq `openai/gpt-oss-120b` verified live
  (`demo_mode: false`, tokens > 0). `llama-3.3-70b-versatile` retired by
  Groq for free/developer tier 2026-08-16 — never switch back.
- **CI**: `.github/workflows/ci.yml` (backend pytest + frontend build).
- **Docker**: `docker-compose.yml` (postgres pgvector, backend,
  frontend, optional `monitoring` profile with real Prometheus) +
  `prometheus.yml`. Not run here — Docker is not installed in this
  environment (verified).

## 2. Partially implemented (PARTIALLY COMPLETED — fixed in this pass)

| # | Gap at audit time | Fix in this pass |
|---|---|---|
| 1 | **No real observational evidence live.** Embedded telemetry store + providers were built (Phase 6, pre-pass) but `emit_scenario_telemetry` was never called, agents never passed `run_id` (cross-case signal leak), no `GET /metrics`, no tests. Every live eval case abstained (run 94c3f97d: semantic 0/5). | Emission wired into `/api/simulate`, worker, eval runner; `run_id=incident.external_id` threaded through logs + metrics agents; `GET /metrics` (Prometheus exposition); `prometheus.yml` scrapes `backend:8000/metrics`; 14 new tests. Status reports providers as `"embedded"`. |
| 2 | **Security gaps.** Legacy fixed-salt password hashes never upgraded; JWT accepted any `alg`/`typ` header shape; `reject` had no state guard; `approve` ignored expiry; no length limits on any input schema; incident title/service entered the LLM prompt bare; PII redaction missed token shapes + Alertmanager descriptions stored raw; `/api/simulate`, `/api/investigate`, `/api/evals/run` (all Groq-costing) had no rate limiting; failed logins not audited; CORS `allow_credentials=True` even with `CORS_ORIGINS="*"`. | All fixed (see §6); 25 new tests in `test_security_review.py`. |
| 3 | **Frontend gaps.** No cold-start state (Render free tier sleeps; first load showed empty lists); eval results + knowledge base rendered as raw JSON dumps; no loading/empty/error states in several views; `api.investigate` unused; unhandled rejection in `open()`; stale footer "No Slack/Jira in v1". | Cold-start "Waking up the backend…" card + retry; typed notice banners; EvalRunCard per run with real latency chart from `per_case[].latency_ms`; knowledge-base tab; "Re-run investigation" button; empty states; tooltips on disabled actions; mobile (~390px) + a11y fixes. |
| 4 | **Stale test assertions.** `test_logs.py` ×2, `test_phase2.py` ×2 asserted the old "no provider" default after the embedded default landed; two Phase 5 fakes lacked the new `run_id` kwarg. | Assertions aligned to `embedded`; fakes updated. No behaviour weakened. |
| 5 | **Docs lagging implementation.** README/docs predated embedded telemetry, the security fixes, and the new frontend. | README + `docs/architecture.md` updated in this pass to the actual implementation. |

## 3. Missing (NOT IMPLEMENTED — documented as future work, not built)

- **Real external observability**: no live Prometheus / Elasticsearch /
  OpenSearch / Grafana / Jira / Slack has ever been connected or tested
  (mocked only). The embedded providers close the loop for the demo;
  wiring a real cluster is operator configuration, not code.
- **AWS / Kubernetes**: out of scope; never in the architecture. No
  code claims otherwise.
- **Proctoring / true DRM**: not applicable to this project.
- **httpOnly-cookie auth**: JWT lives in `localStorage` (XSS-exposed).
  Deliberately not redesigned in this pass — needs a full auth rework
  (backend cookie issuance + frontend rewrite + re-testing the demo
  login flow). Flagged as a dedicated follow-up.
- **Backend lockfile / `pip-audit`**: pins are exact (`==`) but there is
  no lockfile and no offline vulnerability data was available here.
- **Live Postgres test run**: Docker unavailable here, so Postgres as
  primary is code-reviewed only, not run locally.

## 4. Broken APIs / components (found: none outstanding after fixes)

- No broken routes found. All 19 routes exercised by tests or smoke
  tests. One pre-existing design note: `/api/simulate` and
  `/api/incidents/{id}/investigate` are unauthenticated by design (demo
  browsing); they are now rate-limited (10/min/IP) since they trigger
  Groq calls.

## 5. Runtime errors / bugs (fixed in this pass)

- Cross-case telemetry leak (agents read by time window only; two eval
  cases share `checkout-api`) — fixed with `run_id` scoping.
- `approve` on an expired plan succeeded, `execute` then refused it —
  fixed (approve now marks expired, 400).
- `reject` could flip an approved/executed plan to "rejected" — fixed
  (only `pending`/`draft`).
- 1 MB `Content-Length` cap missing (unbounded bodies) — fixed (413).
- Test-suite flakiness: concurrent pytest processes sharing scratch
  SQLite files produce `OperationalError: attempt to write a readonly
  database`. Not a product bug — run the suite once at a time, or in an
  isolated copy. Verified: 157 passed twice in isolation, 157 passed in
  a clean sequential workspace run.

## 6. Security vulnerabilities (reviewed 11 Nov 2026)

| Check | Result |
|---|---|
| Password hashing (PBKDF2-HMAC-SHA256, 100k iters, per-user 128-bit salt) | OK; legacy-hash opportunistic upgrade added |
| JWT (HMAC-SHA256, constant-time compare, expiry, fail-loud demo secret) | OK; header/payload validation hardened (`alg`/`typ`, typed claims, `iat`) |
| Role-based permissions | OK after fixing `approve`/`reject` gaps (§2.2) |
| API access controls (public reads by documented demo design) | OK; no sensitive data leaking in responses |
| CORS | OK after credentials-with-`*` fix |
| Secrets (grep: no hardcoded secrets; `.env` gitignored; no `.env` files in repo) | OK |
| Input sanitization | Fixed: length caps on all schemas + tool inputs; `KnowledgeSearchIn` schema |
| Prompt injection (incident title/service into LLM prompt) | Fixed: sanitise + delimit as untrusted data |
| PII redaction | Extended: token shapes (`gsk_`, `sk-`, `xox*`, `AKIA`, `ghp_/gho_/github_pat_`), `password=`/`secret=` assignments; Alertmanager payloads redacted at insert |
| Rate limiting | Fixed: investigate limiter (10/min/IP) on simulate/investigate/evals/run |
| Audit logging | Fixed: login success/failure + hash upgrades + doc creation logged |
| Approval token + idempotency | Verified correct; token deliberately not mandatory on execute (frontend never sends it; role + approved-status is the enforcement) — now documented in code |
| Executor allow-list | Verified: exactly the 4 tools, dry-run, idempotency, expiry |

Known limitations (not fixed): JWT in `localStorage`; in-memory
per-process rate limiting (behind Render's proxy all clients may share
one IP; `X-Forwarded-For` deliberately not trusted); webhook open by
default unless `ALERT_WEBHOOK_SECRET` is set.

## 7. Poor practices (fixed)

- Unbounded string fields in schemas and tool inputs (→ caps).
- Raw `dict` body on `/api/knowledge/search` (→ typed schema).
- Silent fallback of LLM abstention to deterministic fixtures (fixed in
  Phase 5b, verified intact).
- Stale assertions left behind by the embedded-provider change (§2.4).

## 8. Incomplete UI/UX (fixed in this pass — see §2.3)

Remaining: no live-browser verification of the new UI (build +
typecheck only; needs a browser pass against the live Render backend),
no severity-count chart (data too thin — skipped rather than faked).

## 9. Performance issues

- None found in code review. Notes: Render free tier cold-starts
  (50+ s) are now surfaced honestly in the UI; in-process worker queue
  is the documented scale-out point (Celery+Redis); `loadAll`-style
  N+1 patterns were not found in this codebase. Eval median latency
  1,452 ms (live, 11 Nov 2026).

## 10. Deployment / configuration problems

- **Render**: free tier sleeps; JWT_SECRET must be a real random value
  (fail-loud check refuses to start otherwise) — already set by the
  user-authorised browser step in Phase 4; value not recorded anywhere.
- **Vercel**: Hobby, auto-deploys from `main`.
- **Env vars**: documented in README + `.env.example` files; no secret
  values in the repo (verified by grep).
- **Docker**: compose + Prometheus config present, never run here
  (Docker not installed).

## Honest findings (not patched by design)

- **inventory-cpu telemetry does not distinguish its root cause.**
  Its logs say "batch job inventory-reindex started" / "CPU 96%,
  thread pool saturated"; the guard's support tokens ("runaway",
  "saturating") never appear, and the deterministic semantic classifier
  scores it as `pool_exhaustion` (4) over `cpu_saturation` (3) because
  "thread pool saturated" contains the strong phrase "pool saturated".
  The guard only consults the support check when evidence is docs-only
  or the scenario is unknown, and inventory-cpu is a known scenario
  with real evidence, so the guard will not block a live LLM proposal
  for it — but the LLM itself could misread it. Per the honesty rule
  the telemetry/scenario were NOT edited to match the golden answer.
  Distinguishing signals that do exist: CPU 96% + batch job + low error
  rate (4.2%).
- **Live accuracy is 0 until the post-deploy live eval re-runs.**
  The guard-lift is proven only via mocked-Groq tests + a demo-mode
  smoke test. Do not quote live accuracy numbers until the real
  `POST /api/evals/run` completes after deploy.
