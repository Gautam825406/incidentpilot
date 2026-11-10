"""Prometheus client — Phase 2.

Read-only queries against a configured Prometheus server (GET /api/v1/query).
No metric is ever invented: an empty result, a NaN/Inf value, or a failed
request yields {"available": False, "value": None, ...} and the error is
reported, never a fabricated number.

Auth: an optional Bearer token from settings (PROMETHEUS_AUTH_TOKEN). The
token is only ever sent in the Authorization header — it is never logged,
never included in an error message, and never returned by any API.

PromQL notes (standard selectors on the `service` label; keep queries simple
and documented — if a metric name does not exist in the target Prometheus,
the query returns an empty result and is handled gracefully):
- CPU:    rate of process_cpu_seconds_total (seconds of CPU per second) x100
- Memory: process_resident_memory_bytes (gauge, bytes)
- Errors: 5xx share of http_requests_total over 5m, as a percentage
- Latency: p95 of http_request_duration_seconds_bucket over 5m, in ms
- Availability: the `up` scrape-health metric for the service, as a %
"""
import asyncio
import math

import httpx

from ..config import settings


class PrometheusError(Exception):
    """Raised for invalid responses / connection failures (token never included)."""


def _escape_label(value: str) -> str:
    """Escape a value for use inside a PromQL double-quoted label matcher."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class PrometheusClient:
    def __init__(self, base_url: str | None = None, auth_token: str | None = None,
                 timeout: float | None = None):
        self.base_url = (base_url if base_url is not None else settings.prometheus_url).rstrip("/")
        self._auth_token = auth_token if auth_token is not None else settings.prometheus_auth_token
        self.timeout = float(timeout if timeout is not None else settings.prometheus_timeout)

    def _headers(self) -> dict:
        # Token only ever goes into the header; never logged or echoed.
        if self._auth_token and self._auth_token.strip():
            return {"Authorization": f"Bearer {self._auth_token}"}
        return {}

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        # trust_env=False: ignore proxy env vars. Prometheus is an internal
        # server — routing it through an egress proxy would be wrong, and
        # proxy env parsing itself can fail (httpx.InvalidURL on bracketed
        # IPv6 no_proxy entries). A bad configured URL is a clean error.
        try:
            client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        except httpx.InvalidURL:
            raise PrometheusError("invalid Prometheus URL configured") from None
        async with client:
            return await client.get(f"{self.base_url}{path}", params=params, headers=self._headers())

    async def health(self) -> dict:
        """Return {"reachable": bool, "version": str?, "error": str?}.

        Never raises for a bad server/connection — unreachable Prometheus is
        a normal, reportable state, not an exception.
        """
        if not self.base_url:
            return {"reachable": False, "error": "prometheus_url is not configured"}
        try:
            resp = await self._get("/api/v1/status/buildinfo")
            if resp.status_code != 200:
                return {"reachable": False, "error": f"Prometheus returned HTTP {resp.status_code}"}
            try:
                payload = resp.json()
            except ValueError:
                return {"reachable": False, "error": "invalid JSON response from Prometheus"}
            if not isinstance(payload, dict):
                return {"reachable": False, "error": "invalid response from Prometheus"}
            if payload.get("status") not in (None, "success"):
                return {"reachable": False, "error": f"Prometheus reported status {payload.get('status')!r}"}
            data = payload.get("data", {}) or {}
            out: dict = {"reachable": True}
            if data.get("version"):
                out["version"] = str(data["version"])
            return out
        except PrometheusError as exc:
            return {"reachable": False, "error": str(exc)}
        except Exception:
            # health() never raises: unreachable Prometheus is a reportable
            # state, not an exception (covers httpx errors and bad payloads).
            return {"reachable": False, "error": "connection failed"}

    async def query(self, promql: str) -> list[dict]:
        """Run an instant PromQL query; return the raw data.result list.

        Raises PrometheusError on connection failure, non-2xx, or an invalid
        response shape. Retries twice (3 attempts total) with short backoff
        on 5xx / timeout only; 4xx and bad payloads fail fast.
        """
        if not self.base_url:
            raise PrometheusError("prometheus_url is not configured")
        last_error: str | None = None
        for attempt in range(3):
            try:
                resp = await self._get("/api/v1/query", params={"query": promql})
            except PrometheusError:
                raise
            except httpx.InvalidURL:
                raise PrometheusError("invalid Prometheus URL configured") from None
            except httpx.TimeoutException:
                last_error = "request timed out"
                if attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                raise PrometheusError(last_error) from None
            except httpx.HTTPError:
                raise PrometheusError("connection failed") from None
            if resp.status_code >= 500:
                last_error = f"Prometheus returned HTTP {resp.status_code}"
                if attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                raise PrometheusError(last_error)
            if resp.status_code != 200:
                raise PrometheusError(f"Prometheus returned HTTP {resp.status_code}")
            try:
                payload = resp.json()
            except ValueError:
                raise PrometheusError("invalid JSON response from Prometheus") from None
            if payload.get("status") != "success" or not isinstance(payload.get("data"), dict) \
                    or not isinstance(payload["data"].get("result"), list):
                raise PrometheusError("invalid response from Prometheus")
            return payload["data"]["result"]
        raise PrometheusError(last_error or "query failed")

    # -- metric helpers -------------------------------------------------
    @staticmethod
    def _parse_value(result: list[dict]) -> float | None:
        """First sample value as a float, or None when missing / NaN / Inf."""
        if not result:
            return None
        try:
            value = float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    async def _metric(self, promql: str, unit: str) -> dict:
        try:
            result = await self.query(promql)
        except PrometheusError as exc:
            return {"available": False, "value": None, "unit": unit, "query": promql,
                    "source": "prometheus", "error": str(exc)}
        value = self._parse_value(result)
        if value is None:
            return {"available": False, "value": None, "unit": unit, "query": promql,
                    "source": "prometheus",
                    "error": "no data returned for this metric" if not result else "metric value is not a finite number"}
        return {"available": True, "value": value, "unit": unit, "query": promql,
                "source": "prometheus", "error": None}

    async def query_cpu(self, service: str) -> dict:
        s = _escape_label(service)
        return await self._metric(
            f'avg(rate(process_cpu_seconds_total{{service="{s}"}}[5m])) * 100', "%")

    async def query_memory(self, service: str) -> dict:
        s = _escape_label(service)
        return await self._metric(
            f'avg(process_resident_memory_bytes{{service="{s}"}})', "bytes")

    async def query_error_rate(self, service: str) -> dict:
        s = _escape_label(service)
        return await self._metric(
            f'100 * sum(rate(http_requests_total{{service="{s}",status=~"5.."}}[5m])) '
            f'/ sum(rate(http_requests_total{{service="{s}"}}[5m]))', "%")

    async def query_latency(self, service: str) -> dict:
        s = _escape_label(service)
        return await self._metric(
            f'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket'
            f'{{service="{s}"}}[5m])) by (le)) * 1000', "ms")

    async def query_availability(self, service: str) -> dict:
        s = _escape_label(service)
        return await self._metric(f'avg(up{{service="{s}"}}) * 100', "%")

    async def service_metrics(self, service: str, run_id: str | None = None) -> dict:
        """All five metrics for a service, fetched concurrently.

        ``run_id`` is ignored: a real Prometheus is queried by time
        window, not by simulation run.
        """
        cpu, memory, error_rate, latency, availability = await asyncio.gather(
            self.query_cpu(service),
            self.query_memory(service),
            self.query_error_rate(service),
            self.query_latency(service),
            self.query_availability(service),
        )
        return {"cpu": cpu, "memory": memory, "error_rate": error_rate,
                "latency": latency, "availability": availability}


class EmbeddedPrometheusClient:
    """Phase 6 — metrics emitted by the simulated services themselves.

    Same public interface as :class:`PrometheusClient` (``health``,
    ``query_cpu`` / ``query_memory`` / ``query_error_rate`` /
    ``query_latency`` / ``query_availability``, ``service_metrics``) but
    reads the in-process telemetry store (``app.telemetry``) instead of
    querying an external Prometheus server. Values are COMPUTED from the
    raw observations the simulator recorded (request counters, latency
    samples, CPU gauge) — error rate and p95 are derived, never stored
    as pre-computed answers. Memory is not measured by the simulator, so
    it is always reported unavailable.

    ``run_id`` scopes reads to one simulation run (the incident's
    external_id); without it the store is aggregated across runs.
    ``source`` is "embedded" in every returned metric dict — it never
    pretends to be an external Prometheus.
    """

    source = "embedded"

    def __init__(self):
        from ..telemetry import store as telemetry_store

        self._store = telemetry_store

    async def health(self) -> dict:
        return {"reachable": True}

    def _from_store(self, service: str, name: str, run_id: str | None) -> dict:
        metrics = self._store.service_metrics(service, run_id=run_id)
        entry = metrics.get(name) or {}
        if not entry.get("available"):
            error = ("memory is not measured by the embedded simulator"
                     if name == "memory" else "no data recorded for this metric")
            return {"available": False, "value": None, "unit": entry.get("unit", ""),
                    "query": f"embedded: {name} from telemetry store",
                    "source": "embedded", "error": error}
        return {"available": True, "value": entry["value"], "unit": entry.get("unit", ""),
                "query": f"embedded: {name} from telemetry store",
                "source": "embedded", "error": None}

    async def query_cpu(self, service: str, run_id: str | None = None) -> dict:
        return self._from_store(service, "cpu", run_id)

    async def query_memory(self, service: str, run_id: str | None = None) -> dict:
        return self._from_store(service, "memory", run_id)

    async def query_error_rate(self, service: str, run_id: str | None = None) -> dict:
        return self._from_store(service, "error_rate", run_id)

    async def query_latency(self, service: str, run_id: str | None = None) -> dict:
        return self._from_store(service, "latency", run_id)

    async def query_availability(self, service: str, run_id: str | None = None) -> dict:
        return self._from_store(service, "availability", run_id)

    async def service_metrics(self, service: str, run_id: str | None = None) -> dict:
        """All five metrics for a service, computed from the store."""
        cpu, memory, error_rate, latency, availability = await asyncio.gather(
            self.query_cpu(service, run_id),
            self.query_memory(service, run_id),
            self.query_error_rate(service, run_id),
            self.query_latency(service, run_id),
            self.query_availability(service, run_id),
        )
        return {"cpu": cpu, "memory": memory, "error_rate": error_rate,
                "latency": latency, "availability": availability}


def get_prometheus_client() -> PrometheusClient | EmbeddedPrometheusClient | None:
    """Return a client: the configured external Prometheus when
    PROMETHEUS_URL is set, else the Phase 6 embedded client (telemetry
    emitted by the simulated services). Never None in practice — the
    embedded client is always available — but callers keep the None
    branch defensively."""
    if not settings.prometheus_url or not settings.prometheus_url.strip():
        return EmbeddedPrometheusClient()
    return PrometheusClient()
