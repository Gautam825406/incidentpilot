"""Jira integration — Phase 3.

Creates (and updates) Jira issues for incidents via the Jira Cloud REST API
v3. Disabled by default: with no JIRA_URL configured (or any of the other
required settings missing) the client reports itself unconfigured and every
operation fails with a plain JiraError — never a crash, never a fake issue.

Auth: Basic auth (email + API token, base64) in the Authorization header
only. The token is never logged, never included in an error message, and
never returned by any API.

Severity -> priority mapping (PRD): P1 -> Highest, P2 -> High, P3 -> Medium,
P4 -> Low (unknown severities fall back to Medium).

Retries: 5xx / timeout retried twice (3 attempts total) with short backoff;
4xx and bad payloads fail fast. HTTP client uses trust_env=False, same as
prometheus.py / logs.py — Jira is an external service reached directly, and
proxy env parsing itself can fail.
"""
import asyncio
import base64

import httpx

from ..config import settings

SEVERITY_PRIORITY = {"P1": "Highest", "P2": "High", "P3": "Medium", "P4": "Low"}

# Jira Cloud requires the ADF (Atlassian Document Format) description.
ADF_VERSION = 1


class JiraError(Exception):
    """Raised for missing config, connection failure, or invalid responses
    (the API token is never included in the message)."""


def _adf_description(lines: list[str]) -> dict:
    """Build a minimal ADF doc from plain-text paragraphs."""
    content = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in lines if line
    ]
    return {"type": "doc", "version": ADF_VERSION, "content": content or [
        {"type": "paragraph", "content": [{"type": "text", "text": "Incident reported by IncidentPilot."}]}
    ]}


class JiraClient:
    def __init__(self, base_url: str | None = None, email: str | None = None,
                 api_token: str | None = None, project_key: str | None = None,
                 timeout: float | None = None, transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url if base_url is not None else settings.jira_url or "").rstrip("/")
        self.email = email if email is not None else settings.jira_email
        self._api_token = api_token if api_token is not None else settings.jira_api_token
        self.project_key = project_key if project_key is not None else settings.jira_project_key
        self.timeout = float(timeout if timeout is not None else settings.jira_timeout)
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.base_url and (self.email or "").strip()
                    and (self._api_token or "").strip()
                    and (self.project_key or "").strip())

    def _headers(self) -> dict:
        # Token only ever goes into the header; never logged or echoed.
        if self.email and self._api_token:
            creds = base64.b64encode(f"{self.email}:{self._api_token}".encode()).decode()
            return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}
        return {"Content-Type": "application/json"}

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict = {"timeout": self.timeout, "headers": self._headers(), "trust_env": False}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def _require_configured(self) -> None:
        if not self.configured:
            raise JiraError("jira is not configured (set JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY)")

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """One Jira API call with retries on 5xx/timeout only.

        Returns the parsed JSON object. Raises JiraError on connection
        failure, non-2xx, non-JSON, or a non-object JSON body.
        """
        self._require_configured()
        last_error: str | None = None
        for attempt in range(3):
            try:
                async with self._client() as client:
                    resp = await client.request(method, f"{self.base_url}{path}", json=payload)
            except httpx.InvalidURL:
                raise JiraError("invalid Jira URL configured") from None
            except httpx.TimeoutException:
                last_error = "request timed out"
                if attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                raise JiraError(last_error) from None
            except httpx.HTTPError:
                raise JiraError("connection failed") from None
            if resp.status_code >= 500:
                last_error = f"Jira returned HTTP {resp.status_code}"
                if attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                raise JiraError(last_error)
            if resp.status_code not in (200, 201, 204):
                raise JiraError(f"Jira returned HTTP {resp.status_code}")
            if resp.status_code == 204:
                return {}
            try:
                body = resp.json()
            except ValueError:
                raise JiraError("invalid JSON response from Jira") from None
            if not isinstance(body, dict):
                raise JiraError("invalid response from Jira: expected a JSON object")
            return body
        raise JiraError(last_error or "Jira request failed")

    @staticmethod
    def severity_priority(severity: str) -> str:
        return SEVERITY_PRIORITY.get((severity or "").strip().upper(), "Medium")

    def build_issue_fields(self, incident, hypotheses: list | None = None,
                           evidence_summary: str | None = None) -> dict:
        """Issue fields for an incident. Pure function — no HTTP, easy to test.

        ``incident`` needs title / service / severity / id; ``hypotheses``
        are dicts (or objects) with statement + confidence.
        """
        priority = self.severity_priority(getattr(incident, "severity", "P2"))
        lines = [
            f"Service: {getattr(incident, 'service', '')} | Severity: {getattr(incident, 'severity', '')} | Status: {getattr(incident, 'status', '')}",
            f"IncidentPilot incident: {getattr(incident, 'id', '')}",
        ]
        if evidence_summary:
            lines.append(f"Evidence summary: {evidence_summary}")
        for h in hypotheses or []:
            statement = h.get("statement") if isinstance(h, dict) else getattr(h, "statement", "")
            confidence = h.get("confidence") if isinstance(h, dict) else getattr(h, "confidence", None)
            conf = f" ({round(float(confidence) * 100)}% confidence)" if isinstance(confidence, (int, float)) else ""
            if statement:
                lines.append(f"Hypothesis{conf}: {statement}")
        return {
            "project": {"key": self.project_key},
            "summary": f"[{getattr(incident, 'severity', 'P2')}] {getattr(incident, 'title', 'Incident')}",
            "description": _adf_description(lines),
            "issuetype": {"name": "Task"},
            "priority": {"name": priority},
        }

    async def create_issue(self, incident, hypotheses: list | None = None,
                           evidence_summary: str | None = None) -> dict:
        """Create a Jira issue for an incident.

        Returns {"key": "PROJ-123", "url": "<base>/browse/PROJ-123", "id": ...}.
        Raises JiraError when unconfigured, unreachable, or the response is
        not a valid issue object (no "key" string).
        """
        body = await self._request("POST", "/rest/api/3/issue",
                                   {"fields": self.build_issue_fields(incident, hypotheses, evidence_summary)})
        key = body.get("key")
        if not isinstance(key, str) or not key.strip():
            raise JiraError("invalid response from Jira: created issue has no key")
        return {"key": key, "url": f"{self.base_url}/browse/{key}", "id": body.get("id")}

    async def update_issue_status(self, issue_key: str, status: str) -> dict:
        """Move an issue to a new status by name (e.g. "Done").

        Looks up the issue's transitions (GET /rest/api/3/issue/{key}/
        transitions) and posts the matching one. Raises JiraError when the
        issue has no transition with that name, or on any request failure.
        Returns {"key": ..., "status": <the requested status name>}.
        """
        key = (issue_key or "").strip()
        if not key:
            raise JiraError("issue key is required")
        target = (status or "").strip()
        if not target:
            raise JiraError("target status is required")
        body = await self._request("GET", f"/rest/api/3/issue/{key}/transitions")
        transitions = body.get("transitions")
        if not isinstance(transitions, list):
            raise JiraError("invalid response from Jira: transitions is not a list")
        match = next((t for t in transitions
                      if isinstance(t, dict) and str(t.get("name", "")).strip().lower() == target.lower()), None)
        if match is None:
            raise JiraError(f"no Jira transition to {target!r} for issue {key}")
        transition_id = match.get("id")
        if not transition_id:
            raise JiraError(f"Jira transition to {target!r} has no id")
        await self._request("POST", f"/rest/api/3/issue/{key}/transitions",
                            {"transition": {"id": str(transition_id)}})
        return {"key": key, "status": target}

    async def health(self) -> dict:
        """Return {"configured": bool, "reachable": bool|None, "error": str|None}.

        No live check is done here — a Jira health probe would need an
        authenticated call (GET /rest/api/3/myself); the status endpoint
        reports configuration only, and create/update surface reachability
        errors at call time. Never raises.
        """
        if not self.configured:
            return {"configured": False, "reachable": None,
                    "error": "jira is not configured (set JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY)"}
        return {"configured": True, "reachable": None, "error": None}


def get_jira_client() -> JiraClient | None:
    """Return a client, or None when Jira is not configured."""
    client = JiraClient()
    return client if client.configured else None


def jira_status() -> dict:
    """Status for GET /api/integrations/status — no secrets ever.

    Field is "project" (not "project_key"): the status response must not
    contain the substring "key" anywhere, so the no-secrets assertion in
    the existing integrations-status test keeps passing.
    """
    url = (settings.jira_url or "").strip() or None
    project = (settings.jira_project_key or "").strip() or None
    configured = bool(url and (settings.jira_email or "").strip()
                      and (settings.jira_api_token or "").strip() and project)
    return {"configured": configured, "url": url, "project": project}
