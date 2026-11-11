"""Configuration — all secrets come from environment variables only.

No real secret is ever committed. GROQ_API_KEY is optional: without it
IncidentPilot runs in DEMO MODE (deterministic, fixture-based investigation)
and labels itself as such in the UI and API responses.
"""
import logging
import os
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# The demo JWT secret shipped in code. It is intentionally unchanged so
# existing demo deployments keep working — but a live (non-demo) deployment
# must replace it: see validate_production_settings() below.
DEMO_JWT_SECRET = "demo-secret-change-me-in-production"


class Settings(BaseSettings):
    app_name: str = "IncidentPilot"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Postgres is the primary database path. SQLite is the local/demo
    # fallback only: the default below keeps local dev and the test suite
    # (which uses scratch SQLite files) working with no setup. Set
    # DATABASE_URL to a Postgres URL for any shared/production deployment
    # — see README "Postgres setup (Neon / Supabase free tier)".
    database_url: str = "sqlite:///./incidentpilot.db"
    jwt_secret: str = DEMO_JWT_SECRET
    jwt_algorithm: str = "HS256"
    token_budget_per_incident: int = 60000  # tokens
    max_investigation_rounds: int = 3
    confidence_threshold: float = 0.65
    plan_expiry_minutes: int = 30
    # CORS allow-list (comma-separated). Default covers local dev plus the
    # production frontend; "*" is kept only when explicitly set.
    cors_origins: str = "http://localhost:5173,http://localhost:3000,https://incidentpilot-ten.vercel.app"
    # Rate limiting (in-memory sliding window, per IP; see app/rate_limit.py).
    # Set RATE_LIMIT_ENABLED=false to disable (e.g. behind a proxy that
    # already rate-limits, or in tests — see tests/conftest.py).
    rate_limit_enabled: bool = True
    rate_limit_login_per_minute: int = 10
    rate_limit_webhook_per_minute: int = 60
    # LLM-cost endpoints: /api/simulate, /api/incidents/{id}/investigate
    # and /api/evals/run each trigger Groq calls. Unauthenticated
    # simulate/investigate stay open (the demo path), but per-IP rate
    # limiting caps abuse. See app/main.py investigate_limiter.
    rate_limit_investigate_per_minute: int = 10
    # Phase 2 — Prometheus (optional; empty = not configured, demo mode unaffected)
    prometheus_url: str = ""
    prometheus_auth_token: str = ""
    # Phase 8 — Grafana Cloud Prometheus uses HTTP Basic auth: the
    # instance ID is the username and the API token is the password.
    # Empty = Bearer-token auth (self-hosted Prometheus) as before.
    prometheus_username: str = ""
    prometheus_timeout: float = 10.0
    # Grafana (optional; dashboards is a JSON object mapping service -> dashboard path/UID)
    grafana_url: str = ""
    grafana_dashboards: str = ""
    # Phase 4 — real log providers (optional; empty = not configured).
    # Elasticsearch takes precedence over OpenSearch when both are set.
    elasticsearch_url: str = ""
    elasticsearch_api_key: str = ""
    elasticsearch_index: str = "logs-*"
    opensearch_url: str = ""
    opensearch_auth: str = ""
    opensearch_index: str = "logs-*"
    # Phase 3 — Jira (optional; empty = not configured). The API token is
    # only ever sent in the Authorization header — never logged, never
    # included in an error message, never returned by any API.
    jira_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_project_key: str = ""
    jira_timeout: float = 10.0
    # Phase 3 — Slack incoming webhook (optional; empty = not configured).
    # The webhook URL is only ever POSTed to — never logged or returned.
    slack_webhook_url: str = ""
    slack_channel: str = ""
    slack_timeout: float = 10.0
    # Phase 7 — per-org credential encryption (see app/crypto.py).
    # Base64 urlsafe 32-byte Fernet key. Empty = an ephemeral key is
    # generated at startup (demo only; credentials encrypted with it do
    # not survive a restart). A production deployment (live LLM mode or
    # Postgres DATABASE_URL) REFUSES to start without a persistent key —
    # see validate_credentials_key() in app/crypto.py. Set
    # CREDENTIALS_ENCRYPTION_KEY in production (Render env var).
    credentials_encryption_key: str = ""
    # Phase 8 — optional shared secret for /api/webhooks/alert. Empty
    # (default) keeps the demo behaviour: the webhook is open. When set,
    # callers must send header X-Alert-Token with this value (constant-time
    # compare); without it the webhook returns 401. See main.py.
    alert_webhook_secret: str = ""

    @property
    def demo_mode(self) -> bool:
        return not bool(self.groq_api_key and self.groq_api_key.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        """Parsed CORS allow-list. A bare "*" stays "*" (explicit opt-in
        to wide-open CORS); anything else is a comma-separated list."""
        raw = (self.cors_origins or "").strip()
        if raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def is_postgres(self) -> bool:
        url = (self.database_url or "").strip().lower()
        return url.startswith("postgresql://") or url.startswith("postgres://")

    @property
    def is_sqlite(self) -> bool:
        return (self.database_url or "").strip().lower().startswith("sqlite")

    class Config:
        env_file = ".env"
        extra = "ignore"


def validate_production_settings(s: "Settings") -> None:
    """Fail loudly when a live deployment still uses the demo JWT secret.

    Called once at app startup (main.py imports it). The check is
    deliberately env-driven and import-time: production settings come from
    the environment, which is fixed by the time the app starts. In demo
    mode (no Groq key) the default secret is expected, so it only warns.
    A custom JWT_SECRET always passes, in either mode.
    """
    if s.jwt_secret == DEMO_JWT_SECRET:
        if not s.demo_mode:
            raise RuntimeError(
                "JWT_SECRET is still the demo default but GROQ_API_KEY is set "
                "(live mode). Refusing to start: set a strong, unique "
                "JWT_SECRET environment variable. The demo default must never "
                "sign tokens in production."
            )
        logger.warning(
            "JWT_SECRET is the demo default and the app is in demo mode. "
            "Set a strong JWT_SECRET before enabling live mode."
        )


settings = Settings()
