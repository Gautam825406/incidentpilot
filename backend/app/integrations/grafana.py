"""Grafana helper — Phase 2.

Builds per-service dashboard links from settings only (no secrets involved):
GRAFANA_URL is the base URL and GRAFANA_DASHBOARDS is a JSON object mapping
a service name to a dashboard path/UID, e.g.
    {"checkout-api": "d/abc123/checkout-api", "payments-api": "d/def456/payments-api"}
Empty / unparseable mapping, or a service with no entry, means no link.
"""
import json

from ..config import settings


def _dashboard_map() -> dict:
    raw = (settings.grafana_dashboards or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dashboard_url(service: str) -> str | None:
    """Full Grafana dashboard URL for a service, or None when unconfigured."""
    base = (settings.grafana_url or "").strip()
    if not base:
        return None
    path = _dashboard_map().get(service)
    if not path or not str(path).strip():
        return None
    return f"{base.rstrip('/')}/{str(path).strip().lstrip('/')}"


def grafana_status() -> dict:
    """Return {"configured": bool, "url": str|None} — no secrets ever."""
    url = (settings.grafana_url or "").strip() or None
    return {"configured": bool(url), "url": url}
