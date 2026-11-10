"""Phase 3 tests — Jira + Slack integrations and the /notify endpoint.

All external services are mocked (httpx.MockTransport / monkeypatch) —
no real Jira or Slack is needed. Mirrors test_phase2.py: the DB is a
scratch SQLite file, never the demo incidentpilot.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_phase3.db"
for _f in ("test_phase3.db",):
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _f)
    if os.path.exists(_p):
        os.remove(_p)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.integrations.jira import JiraClient, JiraError, jira_status  # noqa: E402
from app.integrations.slack import SlackClient, SlackError, slack_status  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

JIRA_BASE = "https://example.atlassian.net"


def _jira_client(handler, **overrides):
    kwargs = {"base_url": JIRA_BASE, "email": "ops@example.com",
              "api_token": "test-token-not-real", "project_key": "INC",
              "transport": httpx.MockTransport(handler)}
    kwargs.update(overrides)
    return JiraClient(**kwargs)


def _slack_client(handler, **overrides):
    kwargs = {"webhook_url": "https://hooks.slack.com/services/T/XXX/test",
              "channel": "#incidents",
              "transport": httpx.MockTransport(handler)}
    kwargs.update(overrides)
    return SlackClient(**kwargs)


class _Incident:
    def __init__(self, severity="P1", title="Checkout OOM", service="checkout-api",
                 status="awaiting_approval", id="inc-123"):
        self.severity = severity
        self.title = title
        self.service = service
        self.status = status
        self.id = id


def _auth_headers(email="admin@demo.local", password="admin123"):
    login = client.post("/api/auth/login", json={"email": email, "password": password}).json()
    return {"Authorization": f"Bearer {login['token']}"}


def _make_incident():
    r = client.post("/api/simulate", json={"scenario": "checkout-oom"})
    assert r.status_code == 200, r.text
    return r.json()["incident_id"]


# --- Jira: create -----------------------------------------------------------
@pytest.mark.asyncio
async def test_jira_create_success():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        assert request.method == "POST"
        assert str(request.url) == f"{JIRA_BASE}/rest/api/3/issue"
        import json as _json
        payload = _json.loads(request.content)
        seen["payload"] = payload
        return httpx.Response(201, json={"id": "10001", "key": "INC-42",
                                         "self": f"{JIRA_BASE}/rest/api/3/issue/10001"})

    jira = _jira_client(handler)
    result = await jira.create_issue(_Incident(), hypotheses=[
        {"statement": "Memory leak in deploy v2.15.0", "confidence": 0.91}],
        evidence_summary="7 evidence items linked")
    assert result == {"key": "INC-42", "url": f"{JIRA_BASE}/browse/INC-42", "id": "10001"}
    # Basic auth header present; the raw token never appears in the body.
    assert seen["auth"].startswith("Basic ")
    assert "test-token-not-real" not in str(seen["payload"])
    fields = seen["payload"]["fields"]
    assert fields["project"] == {"key": "INC"}
    assert fields["priority"] == {"name": "Highest"}  # P1 -> Highest
    assert fields["issuetype"] == {"name": "Task"}
    assert fields["summary"].startswith("[P1]")
    assert "Memory leak" in str(fields["description"])


def test_jira_severity_priority_mapping():
    assert JiraClient.severity_priority("P1") == "Highest"
    assert JiraClient.severity_priority("P2") == "High"
    assert JiraClient.severity_priority("P3") == "Medium"
    assert JiraClient.severity_priority("P4") == "Low"
    assert JiraClient.severity_priority("P9") == "Medium"  # unknown -> Medium
    assert JiraClient.severity_priority("") == "Medium"


@pytest.mark.asyncio
async def test_jira_missing_config():
    jira = JiraClient(base_url="", email="", api_token="", project_key="")
    assert jira.configured is False
    with pytest.raises(JiraError, match="not configured"):
        await jira.create_issue(_Incident())
    with pytest.raises(JiraError, match="not configured"):
        await jira.update_issue_status("INC-1", "Done")
    health = await jira.health()
    assert health == {"configured": False, "reachable": None, "error": health["error"]}
    assert "not configured" in health["error"]


@pytest.mark.asyncio
async def test_jira_invalid_response_no_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "10001"})  # no "key"

    with pytest.raises(JiraError, match="no key"):
        await _jira_client(handler).create_issue(_Incident())


@pytest.mark.asyncio
async def test_jira_invalid_response_non_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"not json")

    with pytest.raises(JiraError, match="invalid JSON"):
        await _jira_client(handler).create_issue(_Incident())


@pytest.mark.asyncio
async def test_jira_4xx_fails_fast_no_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"errorMessages": ["bad project"]})

    with pytest.raises(JiraError, match="HTTP 400"):
        await _jira_client(handler).create_issue(_Incident())
    assert calls["n"] == 1  # 4xx fails fast


@pytest.mark.asyncio
async def test_jira_timeout_retries_three_times():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(JiraError, match="timed out"):
        await _jira_client(handler).create_issue(_Incident())
    assert calls["n"] == 3  # 1 attempt + 2 retries


@pytest.mark.asyncio
async def test_jira_5xx_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={})

    with pytest.raises(JiraError, match="HTTP 503"):
        await _jira_client(handler).create_issue(_Incident())
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_jira_update_issue_status_success():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json={"transitions": [
                {"id": "21", "name": "In Progress"}, {"id": "31", "name": "Done"}]})
        assert request.method == "POST"
        import json as _json
        assert _json.loads(request.content) == {"transition": {"id": "31"}}
        return httpx.Response(204)  # Jira transition POST returns 204 No Content

    result = await _jira_client(handler).update_issue_status("INC-42", "Done")
    assert result == {"key": "INC-42", "status": "Done"}
    assert calls[0] == ("GET", f"{JIRA_BASE}/rest/api/3/issue/INC-42/transitions")
    assert calls[1] == ("POST", f"{JIRA_BASE}/rest/api/3/issue/INC-42/transitions")


@pytest.mark.asyncio
async def test_jira_update_issue_status_no_such_transition():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"transitions": [{"id": "21", "name": "In Progress"}]})

    with pytest.raises(JiraError, match="no Jira transition to 'Done'"):
        await _jira_client(handler).update_issue_status("INC-42", "Done")


# --- Slack ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_slack_post_success():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        import json as _json
        seen["payload"] = _json.loads(request.content)
        return httpx.Response(200, content=b"ok")

    result = await _slack_client(handler).post_message("hello incidents")
    assert result == {"delivered": True, "channel": "#incidents"}
    assert seen["url"] == "https://hooks.slack.com/services/T/XXX/test"
    assert seen["payload"]["text"] == "hello incidents"
    assert seen["payload"]["channel"] == "#incidents"
    # The webhook URL is never echoed back in the result.
    assert "hooks.slack.com" not in str(result)


@pytest.mark.asyncio
async def test_slack_missing_config():
    slack = SlackClient(webhook_url="")
    assert slack.configured is False
    with pytest.raises(SlackError, match="not configured"):
        await slack.post_message("hi")
    with pytest.raises(SlackError, match="not configured"):
        await slack.notify("new_incident", _Incident())
    health = await slack.health()
    assert health["configured"] is False
    assert health["reachable"] is None


@pytest.mark.asyncio
async def test_slack_failure_5xx_retries():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, content=b"error")

    with pytest.raises(SlackError, match="HTTP 500"):
        await _slack_client(handler).post_message("hi")
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_slack_failure_non_ok_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"invalid_payload")

    with pytest.raises(SlackError, match="did not acknowledge"):
        await _slack_client(handler).post_message("hi")


@pytest.mark.asyncio
async def test_slack_timeout_retries_three_times():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(SlackError, match="timed out"):
        await _slack_client(handler).post_message("hi")
    assert calls["n"] == 3


def test_slack_build_message_includes_severity_service_confidence():
    payload = SlackClient.build_message(
        "hypothesis_ready", _Incident(),
        hypothesis={"statement": "Memory leak in deploy v2.15.0", "confidence": 0.91},
        app_url="http://localhost:5173")
    text = payload["text"]
    assert "checkout-api" in text
    assert "P1" in text
    assert "91% confidence" in text
    assert "Memory leak in deploy v2.15.0" in text
    assert "http://localhost:5173/incidents/inc-123" in text


@pytest.mark.asyncio
async def test_slack_notify_unknown_event():
    with pytest.raises(SlackError, match="unknown Slack event"):
        await _slack_client(lambda r: httpx.Response(200, content=b"ok")).notify(
            "bogus_event", _Incident())


# --- /api/incidents/{id}/notify endpoint ------------------------------------
def test_notify_requires_auth():
    inc_id = _make_incident()
    assert client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "jira"}).status_code == 401
    assert client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "slack"}).status_code == 401


def test_notify_viewer_forbidden():
    inc_id = _make_incident()
    headers = _auth_headers("viewer@demo.local", "viewer123")
    assert client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "jira"}, headers=headers).status_code == 403
    assert client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "slack"}, headers=headers).status_code == 403


def test_notify_unknown_incident_404():
    headers = _auth_headers()
    assert client.post("/api/incidents/no-such-id/notify", json={"channel": "jira"}, headers=headers).status_code == 404


def test_notify_invalid_channel_422():
    inc_id = _make_incident()
    headers = _auth_headers()
    assert client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "email"}, headers=headers).status_code == 422
    assert client.post(f"/api/incidents/{inc_id}/notify", json={}, headers=headers).status_code == 422


def test_notify_jira_unconfigured_503(monkeypatch):
    monkeypatch.setattr(settings, "jira_url", "")
    monkeypatch.setattr(settings, "jira_email", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    monkeypatch.setattr(settings, "jira_project_key", "")
    inc_id = _make_incident()
    r = client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "jira"},
                    headers=_auth_headers())
    assert r.status_code == 503, r.text
    assert "not configured" in r.json()["detail"]


def test_notify_slack_unconfigured_503(monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", "")
    inc_id = _make_incident()
    r = client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "slack"},
                    headers=_auth_headers())
    assert r.status_code == 503, r.text
    assert "not configured" in r.json()["detail"]


def test_notify_jira_success_links_issue_to_incident(monkeypatch):
    monkeypatch.setattr(settings, "jira_url", JIRA_BASE)
    monkeypatch.setattr(settings, "jira_email", "ops@example.com")
    monkeypatch.setattr(settings, "jira_api_token", "test-token-not-real")
    monkeypatch.setattr(settings, "jira_project_key", "INC")
    calls = {"n": 0}

    async def fake_create(self, incident, hypotheses=None, evidence_summary=None):
        calls["n"] += 1
        assert incident.service == "checkout-api"
        assert hypotheses and hypotheses[0]["statement"].startswith("Memory leak")
        return {"key": "INC-7", "url": f"{JIRA_BASE}/browse/INC-7", "id": "10007"}

    monkeypatch.setattr(JiraClient, "create_issue", fake_create)
    inc_id = _make_incident()
    headers = _auth_headers()
    r = client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "jira"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "channel": "jira", "issue_key": "INC-7",
                        "issue_url": f"{JIRA_BASE}/browse/INC-7", "already_linked": False}
    detail = client.get(f"/api/incidents/{inc_id}").json()
    assert detail["jira_issue_key"] == "INC-7"
    assert detail["jira_issue_url"] == f"{JIRA_BASE}/browse/INC-7"
    # Second call is idempotent — no duplicate Jira issue is created.
    r2 = client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "jira"}, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["already_linked"] is True
    assert r2.json()["issue_key"] == "INC-7"
    assert calls["n"] == 1


def test_notify_slack_success(monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.com/services/T/XXX/test")
    calls = {"n": 0}

    async def fake_notify(self, event, incident, hypothesis=None, detail=None, app_url=None):
        calls["n"] += 1
        assert event == "hypothesis_ready"  # simulate produced a hypothesis
        assert hypothesis and hypothesis["confidence"] >= 0.65
        return {"delivered": True, "channel": None}

    monkeypatch.setattr(SlackClient, "notify", fake_notify)
    inc_id = _make_incident()
    r = client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "slack"},
                    headers=_auth_headers())
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "channel": "slack", "delivered": True}
    assert calls["n"] == 1


def test_notify_jira_failure_502(monkeypatch):
    monkeypatch.setattr(settings, "jira_url", JIRA_BASE)
    monkeypatch.setattr(settings, "jira_email", "ops@example.com")
    monkeypatch.setattr(settings, "jira_api_token", "test-token-not-real")
    monkeypatch.setattr(settings, "jira_project_key", "INC")

    async def fake_create(self, incident, hypotheses=None, evidence_summary=None):
        raise JiraError("Jira returned HTTP 400")

    monkeypatch.setattr(JiraClient, "create_issue", fake_create)
    inc_id = _make_incident()
    r = client.post(f"/api/incidents/{inc_id}/notify", json={"channel": "jira"},
                    headers=_auth_headers())
    assert r.status_code == 502, r.text
    assert "HTTP 400" in r.json()["detail"]
    # Failed creation must not link anything to the incident.
    assert client.get(f"/api/incidents/{inc_id}").json()["jira_issue_key"] is None


# --- /api/integrations/status includes jira + slack -------------------------
def test_integrations_status_includes_jira_and_slack(monkeypatch):
    monkeypatch.setattr(settings, "jira_url", "")
    monkeypatch.setattr(settings, "jira_email", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    monkeypatch.setattr(settings, "jira_project_key", "")
    monkeypatch.setattr(settings, "slack_webhook_url", "")
    monkeypatch.setattr(settings, "slack_channel", "")
    r = client.get("/api/integrations/status")
    assert r.status_code == 200
    body = r.json()
    assert body["jira"] == {"configured": False, "url": None, "project": None}
    assert body["slack"] == {"configured": False, "channel": None}
    # No secret material is ever exposed here.
    assert "token" not in r.text.lower()
    assert "secret" not in r.text.lower()
    assert "key" not in r.text.lower()
    assert "hooks.slack.com" not in r.text


def test_integrations_status_jira_slack_configured_reported(monkeypatch):
    monkeypatch.setattr(settings, "jira_url", JIRA_BASE)
    monkeypatch.setattr(settings, "jira_email", "ops@example.com")
    monkeypatch.setattr(settings, "jira_api_token", "test-token-not-real")
    monkeypatch.setattr(settings, "jira_project_key", "INC")
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.com/services/T/XXX/test")
    monkeypatch.setattr(settings, "slack_channel", "#incidents")
    body = client.get("/api/integrations/status").json()
    assert body["jira"] == {"configured": True, "url": JIRA_BASE, "project": "INC"}
    assert body["slack"] == {"configured": True, "channel": "#incidents"}
    # Even when configured, no credential value is ever returned.
    assert "test-token-not-real" not in str(body)
    assert "hooks.slack.com" not in str(body)


def test_status_helpers_never_expose_secrets(monkeypatch):
    monkeypatch.setattr(settings, "jira_url", JIRA_BASE)
    monkeypatch.setattr(settings, "jira_email", "ops@example.com")
    monkeypatch.setattr(settings, "jira_api_token", "test-token-not-real")
    monkeypatch.setattr(settings, "jira_project_key", "INC")
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.com/services/T/XXX/test")
    monkeypatch.setattr(settings, "slack_channel", "#incidents")
    assert jira_status() == {"configured": True, "url": JIRA_BASE, "project": "INC"}
    assert slack_status() == {"configured": True, "channel": "#incidents"}
    # Neither helper ever returns a credential value.
    assert "test-token-not-real" not in str(jira_status())
    assert "hooks.slack.com" not in str(slack_status())
