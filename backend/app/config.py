"""Configuration — all secrets come from environment variables only.

No real secret is ever committed. GROQ_API_KEY is optional: without it
IncidentPilot runs in DEMO MODE (deterministic, fixture-based investigation)
and labels itself as such in the UI and API responses.
"""
import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "IncidentPilot"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    database_url: str = "sqlite:///./incidentpilot.db"
    jwt_secret: str = "demo-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"
    token_budget_per_incident: int = 60000  # tokens
    max_investigation_rounds: int = 3
    confidence_threshold: float = 0.65
    plan_expiry_minutes: int = 30
    cors_origins: str = "*"
    # Phase 2 — Prometheus (optional; empty = not configured, demo mode unaffected)
    prometheus_url: str = ""
    prometheus_auth_token: str = ""
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
    # Phase 8 — optional shared secret for /api/webhooks/alert. Empty
    # (default) keeps the demo behaviour: the webhook is open. When set,
    # callers must send header X-Alert-Token with this value (constant-time
    # compare); without it the webhook returns 401. See main.py.
    alert_webhook_secret: str = ""

    @property
    def demo_mode(self) -> bool:
        return not bool(self.groq_api_key and self.groq_api_key.strip())

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
