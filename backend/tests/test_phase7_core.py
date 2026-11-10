"""Phase 7 Step 1 tests — SaaS multi-tenancy core.

Covers: signup creates org + admin + token carrying org_id; duplicate
email rejected; login token carries org_id; cross-org incident read /
approve / execute are 404 (not 403); unauthenticated reads see the demo
org only; member invite by admin works and by responder/viewer is 403;
per-org webhook token routes alerts to the right org with per-org dedup;
webhook token rotate returns the token once and the old token stops
working; demo logins still work; crypto + org_integrations helpers
(encrypt at rest, masked views, decrypted view for Worker B).

All mocked / local — no real Groq, Postgres or external services.
"""
import base64
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_phase7.db"
for _f in ("test_phase7.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.crypto import decrypt_secret, encrypt_secret, mask_secret  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Organization, OrganizationMembership, User  # noqa: E402
from app.org_integrations import (  # noqa: E402
    delete_org_integration,
    get_org_integration,
    get_org_integration_decrypted,
    org_integration_status,
    upsert_org_integration,
)

client = TestClient(app)


def _uniq():
    return uuid.uuid4().hex[:8]


def _decode_token(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _signup(company="Acme"):
    suffix = _uniq()
    email = f"admin-{suffix}@acme-{suffix}.example"
    r = client.post("/api/auth/signup", json={
        "company_name": f"{company} {suffix}", "admin_name": "Acme Admin",
        "email": email, "password": "strong-pass-123"})
    assert r.status_code == 200, r.text
    body = r.json()
    return {"email": email, "password": "strong-pass-123", "token": body["token"],
            "org": body["organization"], "user": body["user"]}


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _demo_login():
    r = client.post("/api/auth/login", json={"email": "admin@demo.local", "password": "admin123"})
    assert r.status_code == 200, r.text
    return r.json()


def _simulate_as(token: str):
    r = client.post("/api/simulate", json={"scenario": "checkout-oom"}, headers=_headers(token))
    assert r.status_code == 200, r.text
    return r.json()


# --- Signup / login ---------------------------------------------------------
def test_signup_creates_org_admin_and_token_with_org_id():
    org_user = _signup()
    assert org_user["org"]["name"].startswith("Acme")
    assert org_user["org"]["slug"].startswith("acme-")
    assert org_user["org"]["plan"] == "free"
    assert org_user["user"]["role"] == "admin"
    payload = _decode_token(org_user["token"])
    assert payload["org_id"] == org_user["org"]["id"]
    db = SessionLocal()
    try:
        membership = db.query(OrganizationMembership).filter_by(
            org_id=org_user["org"]["id"]).first()
        assert membership is not None and membership.role == "admin"
        user = db.query(User).filter_by(email=org_user["email"]).first()
        assert user is not None and user.org_id == org_user["org"]["id"]
    finally:
        db.close()


def test_signup_duplicate_email_rejected():
    org_user = _signup()
    r = client.post("/api/auth/signup", json={
        "company_name": "Other Co", "admin_name": "Other",
        "email": org_user["email"], "password": "strong-pass-123"})
    assert r.status_code == 400, r.text


def test_signup_rejects_short_password():
    r = client.post("/api/auth/signup", json={
        "company_name": "Short PW Co", "admin_name": "X",
        "email": f"short-{_uniq()}@example.com", "password": "short"})
    assert r.status_code == 422, r.text


def test_login_token_carries_org_id():
    org_user = _signup()
    r = client.post("/api/auth/login", json={"email": org_user["email"], "password": org_user["password"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert _decode_token(body["token"])["org_id"] == org_user["org"]["id"]
    assert body["organization"]["id"] == org_user["org"]["id"]


def test_demo_login_still_works_and_is_demo_org():
    body = _demo_login()
    assert body["user"]["role"] == "admin"
    assert body["organization"]["slug"] == "demo"
    assert _decode_token(body["token"])["org_id"] == body["organization"]["id"]
    # Demo logins for the other seeded roles still work too.
    for email, password in (("responder@demo.local", "responder123"),
                            ("viewer@demo.local", "viewer123")):
        r = client.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        assert r.json()["organization"]["slug"] == "demo"


def test_org_me_returns_org_role_and_memberships():
    org_user = _signup()
    r = client.get("/api/orgs/me", headers=_headers(org_user["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["organization"]["id"] == org_user["org"]["id"]
    assert body["role"] == "admin"
    assert any(m["org_id"] == org_user["org"]["id"] and m["role"] == "admin"
               for m in body["memberships"])
    # Admin sees the masked webhook token (never the full token).
    assert body["organization"]["webhook_token_masked"].startswith("••••")


# --- Cross-org isolation ----------------------------------------------------
def test_cross_org_incident_is_404_for_read_approve_execute():
    org_a = _signup("OrgA")
    org_b = _signup("OrgB")
    sim = _simulate_as(org_b["token"])
    inc_id = sim["incident_id"]
    plan_id = client.get(f"/api/incidents/{inc_id}", headers=_headers(org_b["token"])).json()["plan"]["id"]
    # Org A cannot read it (404, not 403 — existence must not leak).
    assert client.get(f"/api/incidents/{inc_id}", headers=_headers(org_a["token"])).status_code == 404
    assert client.get(f"/api/incidents/{inc_id}").status_code == 404  # unauthenticated: demo only
    # Org A cannot approve it.
    assert client.post(f"/api/plans/{plan_id}/approve", headers=_headers(org_a["token"])).status_code == 404
    # Org B can approve its own plan.
    assert client.post(f"/api/plans/{plan_id}/approve", headers=_headers(org_b["token"])).status_code == 200
    # Org A cannot execute it even after approval.
    assert client.post(f"/api/plans/{plan_id}/execute", json={"dry_run": True},
                       headers=_headers(org_a["token"])).status_code == 404
    # Org B can.
    assert client.post(f"/api/plans/{plan_id}/execute", json={"dry_run": True},
                       headers=_headers(org_b["token"])).status_code == 200


def test_unauthenticated_list_sees_demo_org_only():
    org_b = _signup("OrgB")
    sim = _simulate_as(org_b["token"])
    ids = [i["id"] for i in client.get("/api/incidents").json()["incidents"]]
    assert sim["incident_id"] not in ids
    # ...but the owning org lists it.
    ids_b = [i["id"] for i in client.get("/api/incidents", headers=_headers(org_b["token"])).json()["incidents"]]
    assert sim["incident_id"] in ids_b


def test_cross_org_investigate_and_verify_are_404():
    org_a = _signup("OrgA")
    org_b = _signup("OrgB")
    sim = _simulate_as(org_b["token"])
    inc_id = sim["incident_id"]
    assert client.post(f"/api/incidents/{inc_id}/investigate",
                       headers=_headers(org_a["token"])).status_code == 404
    assert client.post(f"/api/incidents/{inc_id}/verify",
                       headers=_headers(org_a["token"])).status_code == 404


# --- Members ----------------------------------------------------------------
def test_member_invite_by_admin_works_by_responder_viewer_is_403():
    org = _signup("InviteCo")
    suffix = _uniq()
    new_email = f"member-{suffix}@example.com"
    r = client.post("/api/orgs/members", json={"email": new_email, "role": "responder"},
                    headers=_headers(org["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True
    assert r.json()["user"]["email"] == new_email
    assert r.json()["user"]["role"] == "responder"
    assert "password" not in r.json()["user"]
    assert "password_hash" not in json.dumps(r.json())
    # The new member is listed in the org.
    members = client.get("/api/orgs/members", headers=_headers(org["token"])).json()["members"]
    assert any(m["email"] == new_email for m in members)
    # Demo responder / viewer may not invite (admin-only gate).
    for email, password in (("responder@demo.local", "responder123"),
                            ("viewer@demo.local", "viewer123")):
        login = client.post("/api/auth/login", json={"email": email, "password": password}).json()
        r = client.post("/api/orgs/members", json={"email": f"x-{_uniq()}@example.com", "role": "viewer"},
                        headers=_headers(login["token"]))
        assert r.status_code == 403, r.text


def test_member_invite_existing_user_adds_membership():
    org = _signup("InviteCo2")
    r = client.post("/api/orgs/members", json={"email": "responder@demo.local", "role": "responder"},
                    headers=_headers(org["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["created"] is False
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(email="responder@demo.local").first()
        assert db.query(OrganizationMembership).filter_by(
            org_id=org["org"]["id"], user_id=user.id).first() is not None
    finally:
        db.close()


# --- Per-org webhook ---------------------------------------------------------
def _rotate_token(org):
    r = client.post("/api/orgs/webhook-token/rotate", headers=_headers(org["token"]))
    assert r.status_code == 200, r.text
    return r.json()["webhook_token"]


def test_webhook_token_routes_to_correct_org_and_dedup_is_per_org():
    org_a = _signup("HookA")
    org_b = _signup("HookB")
    token_a = _rotate_token(org_a)
    token_b = _rotate_token(org_b)
    ext = f"p7-shared-{_uniq()}"
    payload = {"title": "shared alert", "service": "checkout-api", "severity": "P1",
               "source": "webhook", "external_id": ext}
    ra = client.post(f"/api/webhooks/alert/{token_a}", json=payload)
    assert ra.status_code == 200, ra.text
    rb = client.post(f"/api/webhooks/alert/{token_b}", json=payload)
    assert rb.status_code == 200, rb.text
    # Same external_id in two orgs = two distinct incidents (per-org dedup).
    assert ra.json()["incident_id"] != rb.json()["incident_id"]
    assert ra.json()["deduplicated"] is False and rb.json()["deduplicated"] is False
    # Repeating in org A dedups within org A.
    ra2 = client.post(f"/api/webhooks/alert/{token_a}", json=payload)
    assert ra2.json()["deduplicated"] is True
    assert ra2.json()["incident_id"] == ra.json()["incident_id"]
    # Each org sees only its own incident.
    assert client.get(f"/api/incidents/{ra.json()['incident_id']}",
                      headers=_headers(org_a["token"])).status_code == 200
    assert client.get(f"/api/incidents/{ra.json()['incident_id']}",
                      headers=_headers(org_b["token"])).status_code == 404
    # Unknown token reveals nothing.
    assert client.post(f"/api/webhooks/alert/{'x' * 43}", json=payload).status_code == 404


def test_webhook_token_rotate_returns_once_and_old_token_stops_working():
    org = _signup("HookRotate")
    token1 = _rotate_token(org)
    token2 = _rotate_token(org)
    assert token1 != token2
    payload = {"title": "rotate probe", "service": "checkout-api", "severity": "P3",
               "source": "webhook", "external_id": f"p7-rotate-{_uniq()}"}
    assert client.post(f"/api/webhooks/alert/{token1}", json=payload).status_code == 404
    assert client.post(f"/api/webhooks/alert/{token2}", json=payload).status_code == 200
    # Rotate is admin-only: demo viewer cannot rotate the demo org token.
    login = client.post("/api/auth/login",
                        json={"email": "viewer@demo.local", "password": "viewer123"}).json()
    assert client.post("/api/orgs/webhook-token/rotate",
                       headers=_headers(login["token"])).status_code == 403


def test_legacy_webhook_scoped_to_caller_org_or_demo():
    org_b = _signup("HookLegacy")
    ext = f"p7-legacy-{_uniq()}"
    payload = {"title": "legacy authed", "service": "checkout-api", "severity": "P2",
               "source": "webhook", "external_id": ext}
    r = client.post("/api/webhooks/alert", json=payload, headers=_headers(org_b["token"]))
    assert r.status_code == 200, r.text
    inc_id = r.json()["incident_id"]
    assert client.get(f"/api/incidents/{inc_id}", headers=_headers(org_b["token"])).status_code == 200
    assert client.get(f"/api/incidents/{inc_id}").status_code == 404  # unauthenticated: demo only
    # Unauthenticated legacy webhook still lands in the demo org.
    ext2 = f"p7-legacy-demo-{_uniq()}"
    r2 = client.post("/api/webhooks/alert", json={
        "title": "legacy demo", "service": "checkout-api", "severity": "P3",
        "source": "webhook", "external_id": ext2})
    assert r2.status_code == 200, r2.text
    assert client.get(f"/api/incidents/{r2.json()['incident_id']}").status_code == 200


# --- Crypto + org integrations (Worker B interface) --------------------------
def test_crypto_roundtrip_and_mask():
    cipher = encrypt_secret("my-token-1234")
    assert cipher != "my-token-1234"
    assert decrypt_secret(cipher) == "my-token-1234"
    assert mask_secret("my-token-1234") == "••••1234"
    assert mask_secret("") == ""
    assert mask_secret("abc") == "••••"
    with pytest.raises(ValueError):
        decrypt_secret("not-a-fernet-token")


def test_org_integration_upsert_encrypts_and_masks():
    org = _signup("IntegCo")
    db = SessionLocal()
    try:
        view = upsert_org_integration(db, org["org"]["id"], "slack", provider="",
                                      config={"channel": "#incidents"},
                                      secrets={"webhook_url": "https://hooks.slack.com/services/SECRET1234"})
        assert view["name"] == "slack"
        assert view["config"] == {"channel": "#incidents"}
        assert view["secrets_masked"]["webhook_url"] == "••••1234"
        assert "SECRET1234" not in json.dumps(view)
        # Stored value is ciphertext, never the plaintext secret.
        row = db.query(__import__("app.models", fromlist=["OrganizationIntegration"]).OrganizationIntegration).filter_by(
            org_id=org["org"]["id"], name="slack").first()
        assert "SECRET1234" not in (row.secret_json or "")
        assert "hooks.slack.com" not in (row.secret_json or "")
        # Masked getter never returns plaintext; decrypted getter does (Worker B path).
        masked = get_org_integration(db, org["org"]["id"], "slack")
        assert masked["secrets_masked"]["webhook_url"] == "••••1234"
        decrypted = get_org_integration_decrypted(db, org["org"]["id"], "slack")
        assert decrypted["secrets"]["webhook_url"] == "https://hooks.slack.com/services/SECRET1234"
        assert decrypted["config"] == {"channel": "#incidents"}
        # Status list carries no secret values at all.
        status = org_integration_status(db, org["org"]["id"])
        slack = [s for s in status if s["name"] == "slack"][0]
        assert slack["configured"] is True
        assert "SECRET1234" not in json.dumps(status)
        assert all(s["name"] in ("prometheus", "logs", "grafana", "jira", "slack") for s in status)
        assert delete_org_integration(db, org["org"]["id"], "slack") is True
        assert get_org_integration(db, org["org"]["id"], "slack") is None
        with pytest.raises(ValueError):
            upsert_org_integration(db, org["org"]["id"], "not-a-service", secrets={})
    finally:
        db.close()


def test_documents_and_evals_scoped_by_org():
    org_a = _signup("DocA")
    org_b = _signup("DocB")
    r = client.post("/api/documents", json={"title": f"OrgB runbook {_uniq()}", "kind": "runbook",
                                            "service": "checkout-api", "content": "secret-b"},
                    headers=_headers(org_b["token"]))
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    docs_b = [d["id"] for d in client.get("/api/documents", headers=_headers(org_b["token"])).json()["documents"]]
    docs_a = [d["id"] for d in client.get("/api/documents", headers=_headers(org_a["token"])).json()["documents"]]
    assert doc_id in docs_b
    assert doc_id not in docs_a
