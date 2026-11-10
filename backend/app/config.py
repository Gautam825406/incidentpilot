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

    @property
    def demo_mode(self) -> bool:
        return not bool(self.groq_api_key and self.groq_api_key.strip())

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
