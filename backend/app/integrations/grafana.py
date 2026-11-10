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


def dashboard_url_for_org(db, org_id: str | None, service: str) -> str | None:
    """Phase 7 — per-org Grafana dashboard URL.

    When the org has an active ``grafana`` integration, use its config
    (``url`` + ``dashboards`` mapping of service -> dashboard path/UID;
    a ``dashboard_url`` string in config is also accepted as a direct
    per-service override key). Otherwise fall back to the global
    :func:`dashboard_url`. ``org_id`` None means "no org resolved":
    straight to the global fallback. No secrets involved.
    """
    if org_id:
        try:
            from ..org_integrations import get_org_integration_decrypted
            integ = get_org_integration_decrypted(db, org_id, "grafana")
        except Exception:
            integ = None
        if integ:
            config = integ.get("config") or {}
            base = (config.get("url") or config.get("base_url") or "").strip()
            if base:
                dashboards = config.get("dashboards")
                path = None
                if isinstance(dashboards, dict):
                    path = dashboards.get(service)
                if not path:
                    # A config-level "dashboard_url" is a single dashboard
                    # for the org (used when only one service is wired up).
                    path = config.get("dashboard_url")
                if path and str(path).strip():
                    return f"{base.rstrip('/')}/{str(path).strip().lstrip('/')}"
                return None
    return dashboard_url(service)


def grafana_status() -> dict:
    """Return {"configured": bool, "url": str|None} — no secrets ever."""
    url = (settings.grafana_url or "").strip() or None
    return {"configured": bool(url), "url": url}
