"""Real log providers for IncidentPilot — Phase 4.

Provider-based adapter architecture over Elasticsearch / OpenSearch
``_search`` APIs. The Logs agent in ``agents.py`` currently reads synthetic
fixtures from ``synthetic.py``; the intended call site for the real path is
the Logs agent inside ``run_investigation`` in ``agents.py``:

    from .integrations.logs import search_logs_real
    result = await search_logs_real(service, start, end, level="error")
    if result["available"]:
        # feed result["entries"] / result["groups"] into the agent context
        # and persist result["entries"] via to_evidence(...) as Evidence rows
    else:
        # result["error"] explains why — fall back to demo/synthetic logs
        # or abstain; NEVER invent log lines when a provider is configured
        # but unreachable.

This module IS wired into ``agents.py`` (Phase 6): in live mode the Logs
agent calls :func:`search_logs_real` from inside ``run_investigation``;
when no provider is configured, or the provider is unreachable, the
investigation records an ``integration_unavailable`` evidence item and
collects no log evidence — entries are never invented. Demo mode keeps
using the synthetic fixtures in ``synthetic.py``.

Phase 6 — embedded provider: with no Elasticsearch / OpenSearch URL
configured, :func:`get_log_provider` returns :class:`EmbeddedLogProvider`,
which reads the telemetry the simulated services emit into
``app.telemetry`` when a scenario is triggered. It is labelled
"embedded" everywhere and never pretends to be an external cluster.

Configuration (read from ``settings`` with an environment fallback;
credentials are never logged):

- Elasticsearch: ``ELASTICSEARCH_URL``, ``ELASTICSEARCH_API_KEY`` (Bearer),
  ``ELASTICSEARCH_INDEX`` (default ``logs-*``).
- OpenSearch: ``OPENSEARCH_URL``, ``OPENSEARCH_AUTH`` (Basic credentials
  passed as-is in the ``Authorization`` header),
  ``OPENSEARCH_INDEX`` (default ``logs-*``).

All providers raise :class:`LogProviderError` on connection failure or an
invalid response shape — they never return invented log entries.
"""
import asyncio
import os
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

try:
    from ..config import settings
except Exception:  # pragma: no cover - config import should not break the module
    settings = None  # type: ignore[assignment]


class LogProviderError(Exception):
    """Raised when a real log provider cannot be reached or returns bad data."""


@dataclass
class LogEntry:
    timestamp: str
    service: str
    level: str
    message: str
    source: str
    exception_type: Optional[str] = None
    count: int = 1


def _setting(name: str, env_name: str) -> Optional[str]:
    """Read a config value from settings first, falling back to the env var.

    ``settings`` may not (yet) define the field — a parallel change owns
    config.py — so ``getattr`` with a default keeps this module working
    either way. Empty strings count as unset.
    """
    value = None
    if settings is not None:
        value = getattr(settings, name, None)
    if value:
        return str(value)
    env_value = os.environ.get(env_name)
    return env_value or None


def _iso(value: "str | datetime") -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class LogProvider(ABC):
    """Abstract log backend. All methods are async; failures raise
    :class:`LogProviderError` instead of returning invented entries."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...

    @abstractmethod
    async def search(
        self,
        service: str,
        start: "str | datetime",
        end: "str | datetime",
        level: str = "error",
        limit: int = 50,
        run_id: str | None = None,
    ) -> list[LogEntry]:
        """Search log lines. ``run_id`` (Phase 6) optionally scopes the
        search to one simulation run; providers that do not support it
        ignore it and query by time window."""

    @abstractmethod
    async def health(self) -> dict:
        """Return {"reachable": bool, "provider": str, "error": str | None}."""


# --- Shared _search client -------------------------------------------------
class _SearchProvider(LogProvider):
    """Base for Elasticsearch / OpenSearch, which share the _search shape."""

    timeout: float = 10.0
    max_retries: int = 2  # retries on 5xx / timeout, i.e. up to 3 attempts

    def __init__(self, base_url: str, index: str, transport: Any = None):
        self.base_url = base_url.rstrip("/")
        self.index = index
        self._transport = transport

    # -- wiring ------------------------------------------------------------
    def _headers(self) -> dict:
        return {"Content-Type": "application/json"}

    def _client(self) -> httpx.AsyncClient:
        # trust_env=False: ignore proxy env vars, same as prometheus.py.
        # Log backends are internal servers — routing them through an
        # egress proxy would be wrong, and proxy env parsing itself can
        # fail (httpx.InvalidURL on bracketed IPv6 no_proxy entries).
        kwargs: dict[str, Any] = {"timeout": self.timeout, "headers": self._headers(), "trust_env": False}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    # -- query --------------------------------------------------------------
    def _query(self, service: str, start: str, end: str, level: str, limit: int) -> dict:
        return {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"service.keyword": service}},
                        {"range": {"@timestamp": {"gte": start, "lte": end}}},
                        {"term": {"level.keyword": level}},
                    ]
                }
            },
            "size": limit,
            "sort": [{"@timestamp": {"order": "desc"}}],
        }

    def _search_url(self) -> str:
        return f"{self.base_url}/{self.index}/_search"

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _parse_hit(hit: dict) -> LogEntry:
        if not isinstance(hit, dict):
            raise LogProviderError(f"invalid search response: hit is not an object: {hit!r:.200}")
        source = hit.get("_source")
        if not isinstance(source, dict):
            raise LogProviderError("invalid search response: hit has no _source object")
        timestamp = source.get("@timestamp") or source.get("timestamp") or ""
        message = source.get("message") or ""
        exception_type = None
        exc = source.get("exception")
        if isinstance(exc, dict):
            exception_type = exc.get("type")
        elif isinstance(exc, str):
            exception_type = exc
        exception_type = exception_type or source.get("error.type") or source.get("exception_type")
        count = source.get("count", 1)
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 1
        return LogEntry(
            timestamp=str(timestamp),
            service=str(source.get("service") or source.get("service.name") or ""),
            level=str(source.get("level") or source.get("log.level") or ""),
            message=str(message),
            source=str(source.get("source") or source.get("log.source") or ""),
            exception_type=str(exception_type) if exception_type else None,
            count=count,
        )

    @classmethod
    def _parse_response(cls, body: Any) -> list[LogEntry]:
        if not isinstance(body, dict):
            raise LogProviderError(f"invalid search response: expected JSON object, got {type(body).__name__}")
        hits = body.get("hits")
        if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list):
            raise LogProviderError("invalid search response: missing hits.hits list")
        return [cls._parse_hit(h) for h in hits["hits"]]

    # -- transport ----------------------------------------------------------
    async def search(
        self,
        service: str,
        start: "str | datetime",
        end: "str | datetime",
        level: str = "error",
        limit: int = 50,
        run_id: str | None = None,
    ) -> list[LogEntry]:
        # run_id is ignored: a real Elasticsearch/OpenSearch backend is
        # queried by time window, not by simulation run.
        payload = self._query(service, _iso(start), _iso(end), level, limit)
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self._client() as client:
                    resp = await client.post(self._search_url(), json=payload)
                if resp.status_code >= 500:
                    last_error = LogProviderError(
                        f"{self.provider_name} search failed with HTTP {resp.status_code}"
                    )
                else:
                    resp.raise_for_status()
                    try:
                        body = resp.json()
                    except ValueError as exc:
                        raise LogProviderError(f"{self.provider_name} returned non-JSON response") from exc
                    return self._parse_response(body)
            except LogProviderError as exc:
                # Non-retryable shape errors surface immediately; 5xx retries.
                if "invalid search response" in str(exc) or "non-JSON" in str(exc):
                    raise
                last_error = exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = LogProviderError(f"{self.provider_name} unreachable: {exc}")
            except httpx.HTTPStatusError as exc:
                raise LogProviderError(
                    f"{self.provider_name} search failed with HTTP {exc.response.status_code}"
                ) from exc
            if attempt < self.max_retries:
                await asyncio.sleep(0.2 * (attempt + 1))
        raise last_error or LogProviderError(f"{self.provider_name} search failed")

    async def health(self) -> dict:
        try:
            async with self._client() as client:
                resp = await client.get(self.base_url, timeout=5.0)
            if resp.status_code >= 500:
                return {"reachable": False, "provider": self.provider_name,
                        "error": f"HTTP {resp.status_code}"}
            return {"reachable": True, "provider": self.provider_name, "error": None}
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return {"reachable": False, "provider": self.provider_name, "error": str(exc)}


class ElasticsearchProvider(_SearchProvider):
    """Elasticsearch log backend (Bearer API key auth)."""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 index: Optional[str] = None, transport: Any = None):
        base_url = base_url or _setting("elasticsearch_url", "ELASTICSEARCH_URL")
        if not base_url:
            raise LogProviderError("elasticsearch_url is not configured")
        self.api_key = api_key or _setting("elasticsearch_api_key", "ELASTICSEARCH_API_KEY")
        index = index or _setting("elasticsearch_index", "ELASTICSEARCH_INDEX") or "logs-*"
        super().__init__(base_url, index, transport=transport)

    @property
    def provider_name(self) -> str:
        return "elasticsearch"

    def _headers(self) -> dict:
        headers = super()._headers()
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


class OpenSearchProvider(_SearchProvider):
    """OpenSearch log backend (Basic auth passed as-is in Authorization)."""

    def __init__(self, base_url: Optional[str] = None, auth: Optional[str] = None,
                 index: Optional[str] = None, transport: Any = None):
        base_url = base_url or _setting("opensearch_url", "OPENSEARCH_URL")
        if not base_url:
            raise LogProviderError("opensearch_url is not configured")
        self.auth = auth or _setting("opensearch_auth", "OPENSEARCH_AUTH")
        index = index or _setting("opensearch_index", "OPENSEARCH_INDEX") or "logs-*"
        super().__init__(base_url, index, transport=transport)

    @property
    def provider_name(self) -> str:
        return "opensearch"

    def _headers(self) -> dict:
        headers = super()._headers()
        if self.auth:
            headers["Authorization"] = self.auth
        return headers


class EmbeddedLogProvider(LogProvider):
    """Phase 6 — logs emitted by the simulated services themselves.

    Reads the in-process telemetry store (``app.telemetry``) that the
    simulator feeds when a scenario is triggered. This is the DEFAULT
    provider when no Elasticsearch / OpenSearch URL is configured: the
    live agents then have real (simulated-service) log evidence instead
    of nothing. It is labelled "embedded" everywhere — it never pretends
    to be an external Elasticsearch/OpenSearch cluster.

    ``run_id`` scopes the search to one simulation run (the incident's
    external_id); without it the time window applies, as for a real
    backend. Level matching is exact: level="error" returns error lines
    only — callers that also want warnings query twice and merge.
    """

    @property
    def provider_name(self) -> str:
        return "embedded"

    async def search(
        self,
        service: str,
        start: "str | datetime",
        end: "str | datetime",
        level: str = "error",
        limit: int = 50,
        run_id: str | None = None,
    ) -> list[LogEntry]:
        from ..telemetry import store as telemetry_store

        def _to_dt(value: "str | datetime") -> datetime:
            if isinstance(value, datetime):
                dt = value
            else:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

        rows = telemetry_store.query_logs(
            service, _to_dt(start), _to_dt(end),
            levels=(level.upper(),), run_id=run_id, limit=limit)
        out = []
        for row in rows:
            ts = row["timestamp"]
            out.append(LogEntry(
                timestamp=ts.isoformat().replace("+00:00", "Z") if isinstance(ts, datetime) else str(ts),
                service=row["service"], level=row["level"], message=row["message"],
                source="embedded", exception_type=row.get("exception_type")))
        return out

    async def health(self) -> dict:
        return {"reachable": True, "provider": "embedded", "error": None}


class NoLogProvider(LogProvider):
    """Fallback when no real log provider is configured (demo mode)."""

    @property
    def provider_name(self) -> str:
        return "none"

    async def search(
        self,
        service: str,
        start: "str | datetime",
        end: "str | datetime",
        level: str = "error",
        limit: int = 50,
        run_id: str | None = None,
    ) -> list[LogEntry]:
        raise LogProviderError("no log provider configured")

    async def health(self) -> dict:
        return {"reachable": False, "provider": "none", "error": "no log provider configured"}


def get_log_provider() -> LogProvider:
    """Return the configured provider: Elasticsearch first, then
    OpenSearch, else the Phase 6 embedded provider (telemetry emitted by
    the simulated services). Never logs credentials.

    The embedded provider is the default when nothing external is
    configured — ``NoLogProvider`` is kept for callers that explicitly
    need the "no provider" behaviour (and its unit tests).
    """
    if _setting("elasticsearch_url", "ELASTICSEARCH_URL"):
        return ElasticsearchProvider()
    if _setting("opensearch_url", "OPENSEARCH_URL"):
        return OpenSearchProvider()
    return EmbeddedLogProvider()


def get_log_provider_for_org(db, org_id: str | None, transport: Any = None) -> LogProvider:
    """Phase 7 — per-org log provider.

    When the org has an active ``logs`` integration, build the provider
    named by its ``provider`` field (``elasticsearch`` | ``opensearch``,
    also accepted in ``config["provider"]``) from config (``url``,
    ``index``) + secrets (Elasticsearch: ``api_key`` / ``token``;
    OpenSearch: ``auth`` / ``api_key`` / ``token``). Otherwise fall back
    to the global provider (:func:`get_log_provider`, which is the
    external provider when configured, else the Phase 6 embedded
    provider). Never returns ``NoLogProvider`` via the fallback path —
    the embedded provider is the default when nothing external is
    configured. ``org_id`` None means "no org resolved": straight to the
    global fallback. ``transport`` is a test hook (httpx.MockTransport).
    """
    if org_id:
        try:
            from ..org_integrations import get_org_integration_decrypted
            integ = get_org_integration_decrypted(db, org_id, "logs")
        except Exception:
            integ = None
        if integ:
            config = integ.get("config") or {}
            secrets = integ.get("secrets") or {}
            url = (config.get("url") or config.get("base_url") or "").strip()
            index = (config.get("index") or "").strip() or None
            provider = ((integ.get("provider") or config.get("provider") or "").strip().lower())
            if url and provider == "elasticsearch":
                return ElasticsearchProvider(
                    base_url=url,
                    api_key=(secrets.get("api_key") or secrets.get("token") or "").strip() or None,
                    index=index, transport=transport)
            if url and provider == "opensearch":
                return OpenSearchProvider(
                    base_url=url,
                    auth=(secrets.get("auth") or secrets.get("api_key") or secrets.get("token") or "").strip() or None,
                    index=index, transport=transport)
            if url:
                # Provider name missing/unknown: default to Elasticsearch
                # (the precedence the global selector uses).
                return ElasticsearchProvider(
                    base_url=url,
                    api_key=(secrets.get("api_key") or secrets.get("token") or "").strip() or None,
                    index=index, transport=transport)
    return get_log_provider()


# --- Aggregation / evidence ------------------------------------------------
def _normalise_message(message: str) -> str:
    return re.sub(r"\s+", " ", message.strip().lower())


def group_exceptions(entries: list[LogEntry]) -> list[dict]:
    """Group entries by exception type (or normalised message prefix).

    Returns [{"exception", "count", "first_seen", "last_seen", "examples"}]
    sorted by count descending. ``examples`` holds up to 3 messages.
    """
    groups: dict[str, dict] = {}
    for entry in entries:
        key = entry.exception_type or _normalise_message(entry.message)[:120]
        group = groups.setdefault(key, {
            "exception": key,
            "count": 0,
            "first_seen": entry.timestamp,
            "last_seen": entry.timestamp,
            "examples": [],
        })
        group["count"] += entry.count
        if entry.timestamp < group["first_seen"]:
            group["first_seen"] = entry.timestamp
        if entry.timestamp > group["last_seen"]:
            group["last_seen"] = entry.timestamp
        if len(group["examples"]) < 3 and entry.message not in group["examples"]:
            group["examples"].append(entry.message)
    return sorted(groups.values(), key=lambda g: g["count"], reverse=True)


def to_evidence(entries: list[LogEntry], groups: list[dict], provider: str = "") -> list[dict]:
    """Convert entries (+ group summaries) into Evidence-shaped dicts.

    Each item matches the ``Evidence`` model in ``models.py``:
    kind="log", source_ref=provider, content=message. Group summaries are
    appended after the per-entry items so the investigation can cite both
    the raw lines and the aggregated counts.
    """
    items: list[dict] = []
    for entry in entries:
        items.append({
            "kind": "log",
            "source": provider,
            "timestamp": entry.timestamp,
            "service": entry.service,
            "level": entry.level,
            "content": entry.message,
            "exception_type": entry.exception_type,
        })
    for group in groups:
        items.append({
            "kind": "log",
            "source": provider,
            "timestamp": group.get("last_seen", ""),
            "service": entries[0].service if entries else "",
            "level": "error",
            "content": (
                f"Exception {group['exception']!r} seen {group['count']} time(s) "
                f"between {group.get('first_seen', '')} and {group.get('last_seen', '')}"
            ),
            "exception_type": group["exception"],
        })
    return items


async def _search_with_provider(provider: LogProvider, service: str,
                              start: "str | datetime", end: "str | datetime",
                              level: str = "error", limit: int = 50,
                              run_id: str | None = None) -> dict:
    """Search via an already-resolved provider (shared by
    :func:`search_logs_real` and :func:`search_logs_real_for_org`)."""
    if isinstance(provider, NoLogProvider):
        return {
            "provider": provider.provider_name,
            "available": False,
            "entries": [],
            "groups": [],
            "error": "no log provider configured",
        }
    try:
        entries = await provider.search(service, start, end, level=level,
                                        limit=limit, run_id=run_id)
    except TypeError:
        # Providers that predate the run_id parameter (e.g. a test double
        # with the old search signature) are called without it.
        entries = await provider.search(service, start, end, level=level, limit=limit)
    except LogProviderError as exc:
        return {
            "provider": provider.provider_name,
            "available": False,
            "entries": [],
            "groups": [],
            "error": str(exc),
        }
    groups = group_exceptions(entries)
    return {
        "provider": provider.provider_name,
        "available": True,
        "entries": [asdict(e) for e in entries],
        "groups": groups,
        "error": None,
    }


async def search_logs_real(
    service: str,
    start: "str | datetime",
    end: "str | datetime",
    level: str = "error",
    limit: int = 50,
    run_id: str | None = None,
) -> dict:
    """Search real logs via the configured provider.

    Returns {"provider", "available", "entries", "groups", "error"}.
    ``available`` is False (with ``error`` set and ``entries`` empty) when
    the provider is unreachable — entries are never fabricated in that
    case. With no external provider configured the Phase 6 embedded
    provider answers from the simulated services' own telemetry (which
    may legitimately be empty: ``available`` True, ``entries`` []).

    ``run_id`` scopes the search to one simulation run (the incident's
    external_id); external providers ignore it and query by time window.
    """
    return await _search_with_provider(get_log_provider(), service, start, end,
                                       level=level, limit=limit, run_id=run_id)


async def search_logs_real_for_org(
    db,
    org_id: str | None,
    service: str,
    start: "str | datetime",
    end: "str | datetime",
    level: str = "error",
    limit: int = 50,
    run_id: str | None = None,
    transport: Any = None,
) -> dict:
    """Phase 7 — :func:`search_logs_real` via the org's log provider
    (:func:`get_log_provider_for_org`, falling back to the global
    provider when the org has none). Same return shape as
    :func:`search_logs_real`."""
    provider = get_log_provider_for_org(db, org_id, transport=transport)
    return await _search_with_provider(provider, service, start, end,
                                       level=level, limit=limit, run_id=run_id)
