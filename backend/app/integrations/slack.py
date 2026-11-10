"""Slack integration — Phase 3.

Posts incident notifications to a Slack channel via an incoming webhook
(SLACK_WEBHOOK_URL). Disabled by default: with no webhook URL configured the
client reports itself unconfigured and every post fails with a plain
SlackError — never a crash, never a fake delivery.

The webhook URL is only ever POSTed to. It is never logged, never included
in an error message, and never returned by any API.

Events: new_incident, hypothesis_ready, plan_approved, plan_executed,
resolved. Each message carries severity, service, confidence (when a
hypothesis exists) and the IncidentPilot incident link (when an app base URL
is supplied — otherwise the incident id).

Retries: 5xx / timeout retried twice (3 attempts total) with short backoff;
4xx and a non-"ok" response fail fast. HTTP client uses trust_env=False,
same as prometheus.py / logs.py / jira.py.
"""
import asyncio

import httpx

from ..config import settings

EVENTS = {"new_incident", "hypothesis_ready", "plan_approved", "plan_executed", "resolved"}

_EVENT_TITLE = {
    "new_incident": "🚨 New incident",
    "hypothesis_ready": "🔍 Hypothesis ready",
    "plan_approved": "✅ Remediation plan approved",
    "plan_executed": "⚙️ Remediation plan executed",
    "resolved": "🎉 Incident resolved",
}


class SlackError(Exception):
    """Raised for missing config, connection failure, or a non-"ok"
    webhook response (the webhook URL is never included)."""


class SlackClient:
    def __init__(self, webhook_url: str | None = None, channel: str | None = None,
                 timeout: float | None = None, transport: httpx.BaseTransport | None = None):
        self.webhook_url = webhook_url if webhook_url is not None else settings.slack_webhook_url
        self.channel = channel if channel is not None else settings.slack_channel
        self.timeout = float(timeout if timeout is not None else settings.slack_timeout)
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool((self.webhook_url or "").strip())

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict = {"timeout": self.timeout, "trust_env": False,
                        "headers": {"Content-Type": "application/json"}}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def _require_configured(self) -> None:
        if not self.configured:
            raise SlackError("slack is not configured (set SLACK_WEBHOOK_URL)")

    @staticmethod
    def build_message(event: str, incident, hypothesis: dict | None = None,
                      detail: str | None = None, app_url: str | None = None) -> dict:
        """Build the webhook payload for an event. Pure function — no HTTP.

        ``incident`` needs title / service / severity / status / id.
        ``hypothesis`` is a dict (or object) with statement + confidence.
        """
        title = _EVENT_TITLE.get(event, "IncidentPilot update")
        severity = getattr(incident, "severity", "")
        service = getattr(incident, "service", "")
        status = getattr(incident, "status", "")
        incident_id = getattr(incident, "id", "")
        lines = [f"{title}: {getattr(incident, 'title', 'Incident')}",
                 f"Service: {service} | Severity: {severity} | Status: {status}"]
        if hypothesis:
            statement = hypothesis.get("statement") if isinstance(hypothesis, dict) else getattr(hypothesis, "statement", "")
            confidence = hypothesis.get("confidence") if isinstance(hypothesis, dict) else getattr(hypothesis, "confidence", None)
            if statement:
                conf = f" ({round(float(confidence) * 100)}% confidence)" if isinstance(confidence, (int, float)) else ""
                lines.append(f"Hypothesis{conf}: {statement}")
        if detail:
            lines.append(detail)
        if app_url and incident_id:
            lines.append(f"Incident: {str(app_url).rstrip('/')}/incidents/{incident_id}")
        elif incident_id:
            lines.append(f"Incident id: {incident_id}")
        return {"text": "\n".join(lines)}

    async def post_message(self, text: str, channel: str | None = None) -> dict:
        """POST a plain-text message to the webhook.

        Returns {"delivered": True, "channel": <channel or None>}. Slack
        incoming webhooks answer 200 with body "ok" on success. Raises
        SlackError when unconfigured, unreachable, or the response is not
        a 200 "ok" (the webhook URL is never in the message).
        """
        self._require_configured()
        payload: dict = {"text": text}
        target_channel = (channel if channel is not None else self.channel or "").strip()
        if target_channel:
            payload["channel"] = target_channel
        last_error: str | None = None
        for attempt in range(3):
            try:
                async with self._client() as client:
                    resp = await client.post(self.webhook_url, json=payload)
            except httpx.InvalidURL:
                raise SlackError("invalid Slack webhook URL configured") from None
            except httpx.TimeoutException:
                last_error = "request timed out"
                if attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                raise SlackError(last_error) from None
            except httpx.HTTPError:
                raise SlackError("connection failed") from None
            if resp.status_code >= 500:
                last_error = f"Slack returned HTTP {resp.status_code}"
                if attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                raise SlackError(last_error)
            if resp.status_code != 200:
                raise SlackError(f"Slack returned HTTP {resp.status_code}")
            if resp.text.strip() != "ok":
                raise SlackError("Slack did not acknowledge the message (expected 'ok')")
            return {"delivered": True, "channel": target_channel or None}
        raise SlackError(last_error or "Slack post failed")

    async def notify(self, event: str, incident, hypothesis: dict | None = None,
                     detail: str | None = None, app_url: str | None = None) -> dict:
        """Post a formatted notification for ``event``.

        Raises SlackError for an unknown event or any post failure; returns
        the post_message result on success.
        """
        if event not in EVENTS:
            raise SlackError(f"unknown Slack event {event!r}")
        payload = self.build_message(event, incident, hypothesis, detail, app_url)
        return await self.post_message(payload["text"])

    async def health(self) -> dict:
        """Return {"configured": bool, "reachable": bool|None, "error": str|None}.

        No live check — probing would send a real message. Reachability is
        surfaced at post time. Never raises.
        """
        if not self.configured:
            return {"configured": False, "reachable": None,
                    "error": "slack is not configured (set SLACK_WEBHOOK_URL)"}
        return {"configured": True, "reachable": None, "error": None}


def get_slack_client() -> SlackClient | None:
    """Return a client, or None when Slack is not configured."""
    client = SlackClient()
    return client if client.configured else None


def slack_status() -> dict:
    """Status for GET /api/integrations/status — no secrets ever.

    The webhook URL is never returned; only whether one is configured and
    which channel messages go to.
    """
    configured = bool((settings.slack_webhook_url or "").strip())
    channel = (settings.slack_channel or "").strip() or None
    return {"configured": configured, "channel": channel}
