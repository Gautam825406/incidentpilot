"""Tests for backend/app/integrations/logs.py (Phase 4).

All provider HTTP traffic is mocked with httpx.MockTransport — no real
Elasticsearch/OpenSearch infrastructure is needed.
"""
import httpx
import pytest

from app.integrations import logs as logs_mod
from app.integrations.logs import (
    EmbeddedLogProvider,
    ElasticsearchProvider,
    LogEntry,
    LogProviderError,
    NoLogProvider,
    OpenSearchProvider,
    get_log_provider,
    group_exceptions,
    search_logs_real,
    to_evidence,
)

START = "2026-11-10T14:00:00Z"
END = "2026-11-10T14:15:00Z"


def _es_body(hits):
    return {"took": 3, "hits": {"total": {"value": len(hits)}, "hits": hits}}


def _hit(ts, service, level, message, exception=None, source=None):
    src = {
        "@timestamp": ts,
        "service": service,
        "level": level,
        "message": message,
        "source": source or "app.log",
    }
    if exception:
        src["exception"] = {"type": exception}
    return {"_index": "logs-2026.11.10", "_id": "1", "_source": src}


def _transport_for(handler):
    return httpx.MockTransport(handler)


# (a) ElasticsearchProvider parses mocked _search responses ---------------
@pytest.mark.asyncio
async def test_elasticsearch_parses_entries():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        import json

        body = json.loads(request.content)
        seen["query"] = body
        assert body["query"]["bool"]["must"][0] == {"term": {"service.keyword": "checkout-api"}}
        assert body["size"] == 50
        return httpx.Response(200, json=_es_body([
            _hit("2026-11-10T14:01:12Z", "checkout-api", "ERROR",
                 "java.lang.OutOfMemoryError: Java heap space", "java.lang.OutOfMemoryError"),
            _hit("2026-11-10T14:01:40Z", "checkout-api", "ERROR",
                 "heap usage 98% — GC thrashing"),
        ]))

    provider = ElasticsearchProvider(
        base_url="http://es:9200", api_key="test-key",
        transport=_transport_for(handler),
    )
    entries = await provider.search("checkout-api", START, END)
    assert len(entries) == 2
    assert entries[0].exception_type == "java.lang.OutOfMemoryError"
    assert entries[0].message.startswith("java.lang.OutOfMemoryError")
    assert entries[0].service == "checkout-api"
    assert entries[0].level == "ERROR"
    assert entries[1].exception_type is None
    assert seen["url"] == "http://es:9200/logs-*/_search"
    assert seen["auth"] == "Bearer test-key"
    assert provider.provider_name == "elasticsearch"


# (b) OpenSearchProvider parses mocked _search responses ------------------
@pytest.mark.asyncio
async def test_opensearch_parses_entries():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_es_body([
            _hit("2026-11-10T14:05:02Z", "payments-api", "ERROR",
                 "HikariPool - Connection is not available", "java.sql.SQLTimeoutException"),
        ]))

    provider = OpenSearchProvider(
        base_url="http://os:9200", auth="Basic dXNlcjpwYXNz",
        transport=_transport_for(handler),
    )
    entries = await provider.search("payments-api", START, END)
    assert len(entries) == 1
    assert entries[0].exception_type == "java.sql.SQLTimeoutException"
    assert seen["url"] == "http://os:9200/logs-*/_search"
    assert seen["auth"] == "Basic dXNlcjpwYXNz"
    assert provider.provider_name == "opensearch"


# (c) Zero hits -> empty list, available, nothing invented -----------------
@pytest.mark.asyncio
async def test_zero_hits_returns_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_es_body([]))

    provider = ElasticsearchProvider(
        base_url="http://es:9200", transport=_transport_for(handler)
    )
    entries = await provider.search("search-api", START, END)
    assert entries == []

    # ... and search_logs_real reports available=True with empty entries
    import unittest.mock as mock

    with mock.patch.object(logs_mod, "get_log_provider", return_value=provider):
        result = await search_logs_real("search-api", START, END)
    assert result["available"] is True
    assert result["entries"] == []
    assert result["groups"] == []
    assert result["error"] is None
    assert result["provider"] == "elasticsearch"


# (d) Connection failure raises LogProviderError; health unreachable -------
@pytest.mark.asyncio
async def test_connection_failure_raises_and_health_false():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = ElasticsearchProvider(
        base_url="http://es:9200", transport=_transport_for(handler)
    )
    with pytest.raises(LogProviderError, match="unreachable"):
        await provider.search("checkout-api", START, END)

    health = await provider.health()
    assert health == {"reachable": False, "provider": "elasticsearch",
                      "error": "connection refused"}


@pytest.mark.asyncio
async def test_search_logs_real_unavailable_on_connection_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = OpenSearchProvider(
        base_url="http://os:9200", transport=_transport_for(handler)
    )
    import unittest.mock as mock

    with mock.patch.object(logs_mod, "get_log_provider", return_value=provider):
        result = await search_logs_real("checkout-api", START, END)
    assert result["available"] is False
    assert result["entries"] == []
    assert result["error"] is not None
    assert "unreachable" in result["error"]


# (e) Invalid response shape raises LogProviderError -----------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_body", [
    {"hits": {"total": 0}},                       # hits.hits missing
    {"hits": "not-a-dict"},                       # hits not an object
    ["not", "an", "object"],                      # top level not an object
    {"hits": {"hits": [{"no_source": True}]}},    # hit without _source
])
async def test_invalid_response_shape_raises(bad_body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bad_body)

    provider = ElasticsearchProvider(
        base_url="http://es:9200", transport=_transport_for(handler)
    )
    with pytest.raises(LogProviderError, match="invalid search response"):
        await provider.search("checkout-api", START, END)


@pytest.mark.asyncio
async def test_non_json_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"this is not json")

    provider = ElasticsearchProvider(
        base_url="http://es:9200", transport=_transport_for(handler)
    )
    with pytest.raises(LogProviderError):
        await provider.search("checkout-api", START, END)


@pytest.mark.asyncio
async def test_5xx_retried_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"error": "unavailable"})

    provider = ElasticsearchProvider(
        base_url="http://es:9200", transport=_transport_for(handler)
    )
    with pytest.raises(LogProviderError, match="HTTP 503"):
        await provider.search("checkout-api", START, END)
    assert calls["n"] == 3  # 1 attempt + 2 retries


# (f) No external provider configured -> embedded provider (Phase 6) ------
# The default is the embedded telemetry store, NOT "none": the simulated
# services emit their own logs/metrics, so live agents have real evidence
# instead of nothing. NoLogProvider still exists for callers that
# explicitly need the "no provider" behaviour (tested below).
def test_get_log_provider_embedded_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    # settings has no elasticsearch/opensearch fields (parallel change may
    # add them with empty defaults) — either way getattr must yield falsy.
    provider = get_log_provider()
    assert isinstance(provider, EmbeddedLogProvider)
    assert provider.provider_name == "embedded"


@pytest.mark.asyncio
async def test_no_log_provider_search_raises_and_health():
    provider = NoLogProvider()
    with pytest.raises(LogProviderError, match="no log provider configured"):
        await provider.search("checkout-api", START, END)
    assert await provider.health() == {
        "reachable": False, "provider": "none",
        "error": "no log provider configured",
    }


@pytest.mark.asyncio
async def test_search_logs_real_embedded_empty_when_unconfigured(monkeypatch):
    """Phase 6: with no external provider the embedded provider answers.
    With nothing emitted yet the store is empty — available True (the
    provider itself is up), entries [] (no telemetry, never invented)."""
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    from app.telemetry import store as telemetry_store

    telemetry_store.reset()
    result = await search_logs_real("checkout-api", START, END)
    assert result == {
        "provider": "embedded",
        "available": True,
        "entries": [],
        "groups": [],
        "error": None,
    }


def test_get_log_provider_prefers_elasticsearch(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_URL", "http://es:9200")
    monkeypatch.setenv("OPENSEARCH_URL", "http://os:9200")
    assert isinstance(get_log_provider(), ElasticsearchProvider)


def test_get_log_provider_opensearch(monkeypatch):
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    monkeypatch.setenv("OPENSEARCH_URL", "http://os:9200")
    assert isinstance(get_log_provider(), OpenSearchProvider)


# (g) group_exceptions groups duplicates -----------------------------------
def test_group_exceptions_groups_duplicates():
    entries = [
        LogEntry("2026-11-10T14:01:12Z", "checkout-api", "ERROR",
                 "java.lang.OutOfMemoryError: Java heap space",
                 "app.log", "java.lang.OutOfMemoryError"),
        LogEntry("2026-11-10T14:01:40Z", "checkout-api", "ERROR",
                 "java.lang.OutOfMemoryError: Java heap space (again)",
                 "app.log", "java.lang.OutOfMemoryError"),
        LogEntry("2026-11-10T14:02:31Z", "checkout-api", "ERROR",
                 "request timeout checkout /v1/orders 504", "app.log", None),
        LogEntry("2026-11-10T14:02:45Z", "checkout-api", "ERROR",
                 "request timeout checkout /v1/orders 504", "app.log", None),
        LogEntry("2026-11-10T14:03:00Z", "checkout-api", "ERROR",
                 "request timeout checkout /v1/orders 504", "app.log", None, count=2),
    ]
    groups = group_exceptions(entries)
    assert len(groups) == 2
    # timeout group: 1 + 1 + 2 = 4, OOM group: 2 -> timeout first
    assert groups[0]["exception"] == "request timeout checkout /v1/orders 504"
    assert groups[0]["count"] == 4
    assert groups[0]["first_seen"] == "2026-11-10T14:02:31Z"
    assert groups[0]["last_seen"] == "2026-11-10T14:03:00Z"
    assert len(groups[0]["examples"]) == 1
    assert groups[1]["exception"] == "java.lang.OutOfMemoryError"
    assert groups[1]["count"] == 2
    assert len(groups[1]["examples"]) == 2


def test_group_exceptions_empty():
    assert group_exceptions([]) == []


def test_to_evidence_shape():
    entries = [
        LogEntry("2026-11-10T14:01:12Z", "checkout-api", "ERROR",
                 "java.lang.OutOfMemoryError: Java heap space",
                 "app.log", "java.lang.OutOfMemoryError"),
    ]
    groups = group_exceptions(entries)
    items = to_evidence(entries, groups, provider="elasticsearch")
    assert items[0]["kind"] == "log"
    assert items[0]["source"] == "elasticsearch"
    assert items[0]["service"] == "checkout-api"
    assert items[0]["content"] == "java.lang.OutOfMemoryError: Java heap space"
    assert items[0]["exception_type"] == "java.lang.OutOfMemoryError"
    # group summary appended after per-entry items
    assert len(items) == 2
    assert "seen 1 time(s)" in items[1]["content"]
