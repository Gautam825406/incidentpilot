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
from datetime import datetime
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
    ) -> list[LogEntry]:
        ...

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
    ) -> list[LogEntry]:
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
    ) -> list[LogEntry]:
        raise LogProviderError("no log provider configured")

    async def health(self) -> dict:
        return {"reachable": False, "provider": "none", "error": "no log provider configured"}


def get_log_provider() -> LogProvider:
    """Return the configured provider: Elasticsearch first, then OpenSearch,
    else :class:`NoLogProvider`. Never logs credentials."""
    if _setting("elasticsearch_url", "ELASTICSEARCH_URL"):
        return ElasticsearchProvider()
    if _setting("opensearch_url", "OPENSEARCH_URL"):
        return OpenSearchProvider()
    return NoLogProvider()


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


async def search_logs_real(
    service: str,
    start: "str | datetime",
    end: "str | datetime",
    level: str = "error",
    limit: int = 50,
) -> dict:
    """Search real logs via the configured provider.

    Returns {"provider", "available", "entries", "groups", "error"}.
    ``available`` is False (with ``error`` set and ``entries`` empty) when no
    provider is configured or the provider is unreachable — entries are
    never fabricated in that case.
    """
    provider = get_log_provider()
    if isinstance(provider, NoLogProvider):
        return {
            "provider": provider.provider_name,
            "available": False,
            "entries": [],
            "groups": [],
            "error": "no log provider configured",
        }
    try:
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
