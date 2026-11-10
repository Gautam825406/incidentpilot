"""Per-org integration storage (Phase 7, SaaS multi-tenancy).

This module is the ONLY writer of ``OrganizationIntegration`` rows and
the interface Worker B (per-org integrations in ``agents.py`` /
``integrations/*.py``) must use — see the interface contract at the
bottom of this file's docstring and in the Phase 7 handoff.

Storage rules:
- ``config_json``: NON-secret fields only (url, project, index, channel,
  ...). Stored as plain JSON.
- ``secret_json``: ENCRYPTED secret fields only (token / api_key /
  webhook_url / password / ...). Every value is encrypted with
  ``app.crypto.encrypt_secret`` before storage; plaintext is NEVER
  written to the DB, the logs, or an API response.
- Masked views (what the API returns) show secrets as ``"••••1234"``
  (see ``app.crypto.mask_secret``).

Interface for Worker B (agents.py / integrations):
- ``get_org_integration_decrypted(db, org_id, name)`` -> dict | None
  with ``{"name", "provider", "config": {...}, "secrets": {...decrypted...},
  "is_active", "last_test_ok"}`` — the ONLY way agents obtain an org's
  plaintext integration secrets. Call it per investigation (never cache
  secrets process-wide).
- ``get_org_integration(db, org_id, name)`` -> masked view (safe for API/logs).
- ``org_integration_status(db, org_id)`` -> per-name status list, no secrets.
- ``mark_integration_tested(db, org_id, name, ok)`` -> record a test result.
- When no per-org row exists (or it is inactive), fall back to the
  existing global env-var config in ``settings`` — that keeps the demo
  org and all pre-Phase-7 behaviour working unchanged.
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .crypto import decrypt_secret, encrypt_secret, mask_secret
from .models import OrganizationIntegration

logger = logging.getLogger(__name__)

INTEGRATION_NAMES = ("prometheus", "logs", "grafana", "jira", "slack")

# Fields that are secrets for each integration. Anything in ``secrets``
# passed to upsert that is NOT listed here is still encrypted (defence in
# depth: unknown fields are treated as secrets, never stored plaintext),
# but these lists document the expected keys per integration.
SECRET_FIELDS = {
    "prometheus": {"token", "api_key", "password"},
    "logs": {"api_key", "token", "password"},
    "grafana": {"api_key", "token", "password"},
    "jira": {"api_token", "token", "password"},
    "slack": {"webhook_url", "token"},
}


def _load_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _masked_view(row: OrganizationIntegration) -> dict:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "provider": row.provider or "",
        "config": _load_json(row.config_json),
        # Masked only — never plaintext. (Filled in by callers via
        # _masked_secrets, which decrypts then masks; ciphertext itself
        # is never exposed.)
        "secrets_masked": {},
        "is_active": bool(row.is_active),
        "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
        "last_test_ok": row.last_test_ok,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _masked_secrets(row: OrganizationIntegration) -> dict:
    """Masked secrets dict: keys preserved, values masked (ciphertext is
    never exposed — even the encrypted token is not a display value)."""
    # We cannot mask the plaintext without decrypting; the stored value
    # is ciphertext, so mask the *decrypted* value's tail only when it
    # decrypts cleanly, otherwise show a generic placeholder.
    out = {}
    for key, cipher in _load_json(row.secret_json).items():
        try:
            out[key] = mask_secret(decrypt_secret(cipher))
        except Exception:
            out[key] = "••••"
    return out


def _decrypted_view(row: OrganizationIntegration) -> dict:
    secrets = {}
    for key, cipher in _load_json(row.secret_json).items():
        try:
            secrets[key] = decrypt_secret(cipher)
        except Exception:
            # A secret encrypted with a lost ephemeral key cannot be
            # recovered; surface it as missing rather than failing the
            # whole lookup (callers treat a missing secret as "not
            # configured"). Never log the ciphertext or the key.
            logger.warning("Could not decrypt stored secret for integration %s (org %s); treating as unconfigured",
                           row.name, row.org_id)
            continue
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "provider": row.provider or "",
        "config": _load_json(row.config_json),
        "secrets": secrets,
        "is_active": bool(row.is_active),
        "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
        "last_test_ok": row.last_test_ok,
    }


def _validate_name(name: str) -> str:
    name = (name or "").strip().lower()
    if name not in INTEGRATION_NAMES:
        raise ValueError(f"unknown integration name {name!r}; expected one of {sorted(INTEGRATION_NAMES)}")
    return name


def get_org_integration(db: Session, org_id: str, name: str) -> dict | None:
    """Masked view of one org integration, or None when not configured.

    Safe for API responses and logs: secret values appear only masked.
    """
    row = db.query(OrganizationIntegration).filter_by(org_id=org_id, name=_validate_name(name)).first()
    if not row:
        return None
    view = _masked_view(row)
    view["secrets_masked"] = _masked_secrets(row)
    return view


def get_org_integration_decrypted(db: Session, org_id: str, name: str) -> dict | None:
    """Decrypted view of one org integration, or None when not configured.

    **Worker B interface** — the only sanctioned way for agents.py /
    integrations/*.py to obtain an org's plaintext integration secrets.
    Returns ``{"name", "provider", "config": {...non-secret...},
    "secrets": {...plaintext, decrypted...}, "is_active", "last_test_ok"}``.
    Callers must never log ``secrets`` or return them from an API, and
    must not cache them beyond a single investigation run. Inactive
    rows return None (treat as not configured).
    """
    row = db.query(OrganizationIntegration).filter_by(org_id=org_id, name=_validate_name(name)).first()
    if not row or not row.is_active:
        return None
    return _decrypted_view(row)


def get_all_org_integrations(db: Session, org_id: str, decrypted: bool = False) -> list[dict]:
    """All configured integrations for an org (masked by default).

    Pass ``decrypted=True`` only for internal agent use (same handling
    rules as ``get_org_integration_decrypted``).
    """
    rows = db.query(OrganizationIntegration).filter_by(org_id=org_id).order_by(OrganizationIntegration.name).all()
    out = []
    for row in rows:
        if decrypted:
            out.append(_decrypted_view(row))
        else:
            view = _masked_view(row)
            view["secrets_masked"] = _masked_secrets(row)
            out.append(view)
    return out


def upsert_org_integration(db: Session, org_id: str, name: str, provider: str | None = None,
                           config: dict | None = None, secrets: dict | None = None) -> dict:
    """Create or update one org integration. Secrets are encrypted before
    storage — plaintext is never persisted. ``config`` replaces the stored
    non-secret config when given; ``secrets`` are MERGED by key when
    given (a partial update does not wipe other stored secrets). Pass
    ``secrets={key: ""}`` or ``None`` to leave a stored secret untouched;
    there is intentionally no API to read a secret back in plaintext.

    Returns the masked view (safe for API responses).
    """
    name = _validate_name(name)
    row = db.query(OrganizationIntegration).filter_by(org_id=org_id, name=name).first()
    if row is None:
        row = OrganizationIntegration(org_id=org_id, name=name, provider=provider or "",
                                      config_json="{}", secret_json="{}")
        db.add(row)
    if provider is not None:
        row.provider = provider
    if config is not None:
        row.config_json = json.dumps({str(k): v for k, v in dict(config).items()})
    if secrets:
        stored = _load_json(row.secret_json)
        for key, value in dict(secrets).items():
            if value is None or value == "":
                continue
            # Every secret value is encrypted, whatever its key.
            stored[str(key)] = encrypt_secret(str(value))
        row.secret_json = json.dumps(stored)
    row.is_active = True
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    view = _masked_view(row)
    view["secrets_masked"] = _masked_secrets(row)
    return view


def delete_org_integration(db: Session, org_id: str, name: str) -> bool:
    """Delete one org integration (config + encrypted secrets). Returns
    True when a row existed and was deleted, False otherwise."""
    row = db.query(OrganizationIntegration).filter_by(org_id=org_id, name=_validate_name(name)).first()
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def mark_integration_tested(db: Session, org_id: str, name: str, ok: bool) -> dict | None:
    """Record the outcome of a connectivity test for an integration.
    Returns the masked view, or None when the integration is not configured."""
    row = db.query(OrganizationIntegration).filter_by(org_id=org_id, name=_validate_name(name)).first()
    if not row:
        return None
    row.last_tested_at = datetime.now(timezone.utc)
    row.last_test_ok = bool(ok)
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    view = _masked_view(row)
    view["secrets_masked"] = _masked_secrets(row)
    return view


def org_integration_status(db: Session, org_id: str) -> list[dict]:
    """Per-integration status for an org — one entry for each of the five
    known integrations, with NO secret values (not even masked secrets,
    only a masked url/host when one is configured).

    Each entry: ``{"name", "configured", "provider", "masked_url",
    "last_test_ok"}`` where ``configured`` means a row exists and is
    active, and ``masked_url`` is the configured url/host with any
    embedded credentials stripped and the tail masked (or None).
    """
    rows = {r.name: r for r in db.query(OrganizationIntegration).filter_by(org_id=org_id).all()}
    out = []
    for name in INTEGRATION_NAMES:
        row = rows.get(name)
        if row is None or not row.is_active:
            out.append({"name": name, "configured": False, "provider": row.provider if row else "",
                        "masked_url": None, "last_test_ok": row.last_test_ok if row else None})
            continue
        cfg = _load_json(row.config_json)
        raw_url = cfg.get("url") or cfg.get("host") or cfg.get("endpoint") or ""
        masked_url = mask_secret(str(raw_url)) if raw_url else None
        out.append({"name": name, "configured": True, "provider": row.provider or "",
                    "masked_url": masked_url, "last_test_ok": row.last_test_ok})
    return out
