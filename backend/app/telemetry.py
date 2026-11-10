"""Embedded telemetry store — Phase 6.

The simulated services emit REAL telemetry as they run, into this
in-process store, and the agents read it back through the same provider
interfaces as the external systems (EmbeddedLogProvider /
EmbeddedPrometheusClient). Nothing here is production data: the services
are simulated, so their telemetry is simulated too — but it is MEASURED
from the simulation, not copied from the golden answers:

- ``emit_scenario_telemetry`` takes only the scenario's observable
  signals (its log lines and its service-state metrics). It NEVER sees
  the scenario's ``root_cause`` / ``remediation_tool`` fields, and it
  never writes them anywhere. The agents must infer the cause from the
  signals, exactly as they would from a real Prometheus / log backend.
- Log lines are re-stamped at emission time (the fixture timestamps are
  from 2026-11-10 and would fall outside every query window). The
  message text is the simulated service's output, unchanged.
- Request observations are generated from the scenario's service state:
  ``total`` requests, of which ``error_rate`` percent fail with the
  scenario latency and the rest succeed at the baseline latency. Error
  rate and p95 latency are then COMPUTED from those observations by the
  embedded Prometheus reader — they are not stored as pre-computed
  answers.
- CPU is a gauge sample; ``process_cpu_seconds_total`` accumulates
  ``cpu% * 60s`` per run (the simulated service ran at that CPU level
  for a 60-second observation window). Memory is NOT measured by the
  simulator, so the embedded reader reports it unavailable rather than
  inventing a value.

Run scoping — why telemetry is tagged with a run id:
each simulation run (``/api/simulate`` or one eval case) emits one batch
of telemetry tagged with the incident's ``external_id``. The agents only
read the batch for THEIR incident's run. That is the isolation a
time-window query gives in production, where incidents do not overlap
in the same minute; the eval fires six incidents for five services
within minutes, so a plain 60-minute window would leak one case's
signals into the next (checkout-oom's OutOfMemoryError lines would show
up in the unknown-signal investigation and defeat the abstention
guard). Ad-hoc / Alertmanager alerts have no run, so they read nothing
and correctly abstain when there is no evidence.

The store is bounded (oldest entries/samples dropped past the caps) so
a long-running deployment cannot grow without limit. ``reset()`` exists
for tests.
"""
import re
import threading
from datetime import datetime, timedelta, timezone

MAX_LOG_LINES = 5000
MAX_LATENCY_SAMPLES_PER_SERVICE_RUN = 1000
REQUESTS_PER_RUN = 200
OBSERVATION_WINDOW_SECONDS = 60.0

_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\S+)\s+(?P<service>\S+)\s+(?P<level>ERROR|WARN|INFO|DEBUG)\s+(?P<message>.*)$"
)
_EXCEPTION_RE = re.compile(r"([A-Za-z][A-Za-z0-9_.]*(?:Exception|Error))")


def _parse_log_line(line: str, service: str) -> dict | None:
    """Split a scenario log line into (level, message, exception_type).

    The fixture lines carry a stale timestamp and a service token; both
    are stripped — the store re-stamps at emission time and records the
    service separately. A line that does not match the shape is kept
    verbatim as an INFO line rather than dropped.
    """
    m = _LOG_LINE_RE.match(line.strip())
    if not m:
        return {"level": "INFO", "message": line.strip(), "exception_type": None}
    message = m.group("message").strip()
    exc = _EXCEPTION_RE.search(message)
    return {"level": m.group("level"), "message": message,
            "exception_type": exc.group(1) if exc else None}


class TelemetryStore:
    """In-process store for embedded (simulated-service) telemetry."""

    def __init__(self):
        self._lock = threading.Lock()
        self._logs: list[dict] = []  # {timestamp, service, level, message, exception_type, run_id}
        # (service, run_id) -> {"requests": int, "errors": int,
        #   "latencies": [ms], "cpu": float|None, "cpu_seconds": float}
        self._metrics: dict[tuple[str, str], dict] = {}

    # -- emission ------------------------------------------------------
    def emit_log(self, service: str, level: str, message: str,
                 run_id: str | None = None, timestamp: datetime | None = None,
                 exception_type: str | None = None) -> None:
        with self._lock:
            self._logs.append({
                "timestamp": timestamp or datetime.now(timezone.utc),
                "service": service,
                "level": level,
                "message": message,
                "exception_type": exception_type,
                "run_id": run_id,
            })
            if len(self._logs) > MAX_LOG_LINES:
                del self._logs[: len(self._logs) - MAX_LOG_LINES]

    def emit_requests(self, service: str, run_id: str | None, total: int,
                      errors: int, error_latency_ms: float,
                      ok_latency_ms: float) -> None:
        """Record one batch of request observations for a service/run."""
        with self._lock:
            bucket = self._metrics.setdefault(
                (service, run_id or ""), {"requests": 0, "errors": 0,
                                          "latencies": [], "cpu": None,
                                          "cpu_seconds": 0.0})
            bucket["requests"] += total
            bucket["errors"] += errors
            bucket["latencies"].extend(
                [float(error_latency_ms)] * errors
                + [float(ok_latency_ms)] * (total - errors))
            if len(bucket["latencies"]) > MAX_LATENCY_SAMPLES_PER_SERVICE_RUN:
                bucket["latencies"] = bucket["latencies"][-MAX_LATENCY_SAMPLES_PER_SERVICE_RUN:]

    def set_cpu(self, service: str, run_id: str | None, cpu_percent: float) -> None:
        """Record a CPU gauge sample and accumulate CPU-seconds for the run."""
        with self._lock:
            bucket = self._metrics.setdefault(
                (service, run_id or ""), {"requests": 0, "errors": 0,
                                          "latencies": [], "cpu": None,
                                          "cpu_seconds": 0.0})
            bucket["cpu"] = float(cpu_percent)
            bucket["cpu_seconds"] += (float(cpu_percent) / 100.0) * OBSERVATION_WINDOW_SECONDS

    # -- queries ---------------------------------------------------------
    def query_logs(self, service: str, start: datetime, end: datetime,
                   levels: tuple[str, ...] = ("ERROR",),
                   run_id: str | None = None, limit: int = 50) -> list[dict]:
        """Log lines for a service.

        When ``run_id`` is given, the run tag is the isolation boundary
        and the time window is NOT applied (see module docstring). Without
        a run id the time window applies, as for a real log backend.
        """
        with self._lock:
            rows = [r for r in self._logs
                    if r["service"] == service and r["level"] in levels]
            if run_id is not None:
                rows = [r for r in rows if r["run_id"] == run_id]
            else:
                rows = [r for r in rows if start <= r["timestamp"] <= end]
            rows.sort(key=lambda r: r["timestamp"])
            return [dict(r) for r in rows[:limit]]

    def service_metrics(self, service: str, run_id: str | None = None) -> dict:
        """Computed metrics for a service/run, from the raw observations.

        Returns {"cpu", "memory", "error_rate", "latency", "availability"}
        as {"available", "value", "unit"} dicts. Memory is never measured
        by the simulator, so it is always unavailable.
        """
        with self._lock:
            if run_id is not None:
                buckets = [b for (s, r), b in self._metrics.items()
                           if s == service and r == run_id]
            else:
                buckets = [b for (s, _r), b in self._metrics.items() if s == service]
            if not buckets:
                return {name: {"available": False, "value": None, "unit": unit}
                        for name, unit in [("cpu", "%"), ("memory", "bytes"),
                                           ("error_rate", "%"), ("latency", "ms"),
                                           ("availability", "%")]}
            requests = sum(b["requests"] for b in buckets)
            errors = sum(b["errors"] for b in buckets)
            latencies = sorted(l for b in buckets for l in b["latencies"])
            cpus = [b["cpu"] for b in buckets if b["cpu"] is not None]
            cpu_seconds = sum(b["cpu_seconds"] for b in buckets)
            out = {
                "cpu": {"available": bool(cpus), "value": cpus[-1] if cpus else None, "unit": "%"},
                "memory": {"available": False, "value": None, "unit": "bytes"},
                "error_rate": {"available": requests > 0,
                               "value": (100.0 * errors / requests) if requests else None,
                               "unit": "%"},
                "latency": {"available": bool(latencies),
                            "value": (latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
                                      if latencies else None),
                            "unit": "ms"},
                "availability": {"available": True, "value": 100.0, "unit": "%"},
            }
            out["_cpu_seconds_total"] = cpu_seconds
            out["_requests_total"] = requests
            out["_errors_total"] = errors
            return out

    def prometheus_text(self) -> str:
        """All recorded telemetry in Prometheus exposition format.

        Counters are cumulative across runs (as a real /metrics endpoint
        reports them); per-run isolation is the agents' read path, not the
        scrape path. Memory is omitted: it is not measured.
        """
        with self._lock:
            services: dict[str, dict] = {}
            for (service, _run), b in self._metrics.items():
                agg = services.setdefault(service, {"requests": 0, "errors": 0,
                                                    "latencies": [], "cpu": None,
                                                    "cpu_seconds": 0.0})
                agg["requests"] += b["requests"]
                agg["errors"] += b["errors"]
                agg["latencies"].extend(b["latencies"])
                if b["cpu"] is not None:
                    agg["cpu"] = b["cpu"]
                agg["cpu_seconds"] += b["cpu_seconds"]
            lines = []
            for service in sorted(services):
                agg = services[service]
                ok = agg["requests"] - agg["errors"]
                lines.append("# HELP http_requests_total Total HTTP requests by service and status.")
                lines.append("# TYPE http_requests_total counter")
                lines.append(f'http_requests_total{{service="{service}",status="200"}} {ok}')
                lines.append(f'http_requests_total{{service="{service}",status="500"}} {agg["errors"]}')
                lines.append("# HELP http_request_duration_seconds Request latency histogram.")
                lines.append("# TYPE http_request_duration_seconds histogram")
                lat = sorted(agg["latencies"])
                total = len(lat)
                for le in ("0.1", "0.25", "0.5", "1", "2.5", "5"):
                    count = sum(1 for v in lat if v / 1000.0 <= float(le))
                    lines.append(f'http_request_duration_seconds_bucket{{service="{service}",le="{le}"}} {count}')
                lines.append(f'http_request_duration_seconds_bucket{{service="{service}",le="+Inf"}} {total}')
                lines.append(f'http_request_duration_seconds_sum{{service="{service}"}} {sum(lat) / 1000.0}')
                lines.append(f'http_request_duration_seconds_count{{service="{service}"}} {total}')
                lines.append("# HELP process_cpu_seconds_total Total CPU time consumed in seconds.")
                lines.append("# TYPE process_cpu_seconds_total counter")
                lines.append(f'process_cpu_seconds_total{{service="{service}"}} {agg["cpu_seconds"]}')
                if agg["cpu"] is not None:
                    lines.append("# HELP service_cpu_percent Latest observed CPU utilisation.")
                    lines.append("# TYPE service_cpu_percent gauge")
                    lines.append(f'service_cpu_percent{{service="{service}"}} {agg["cpu"]}')
                lines.append("# HELP up Whether the simulated service is up (1) or down (0).")
                lines.append("# TYPE up gauge")
                lines.append(f'up{{service="{service}"}} 1')
            return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        with self._lock:
            self._logs.clear()
            self._metrics.clear()


# Module-level singleton: one store per process, shared by the simulator
# (emission) and the embedded providers (reads).
store = TelemetryStore()


def emit_scenario_telemetry(scenario: dict, run_id: str) -> dict:
    """Emit a scenario's observable signals into the store.

    Only the scenario's ``logs`` and ``metrics`` are read — the
    ``root_cause`` / ``remediation_tool`` / ``unsafe_action`` fields are
    deliberately never touched, so the emitted telemetry cannot carry the
    expected answer. Returns {"log_lines": n, "requests": n}.
    """
    service = scenario["service"]
    now = datetime.now(timezone.utc)
    lines = scenario.get("logs") or []
    for i, line in enumerate(lines):
        parsed = _parse_log_line(line, service)
        # Re-stamp at emission time, newest last, all within the last minute.
        ts = now - timedelta(seconds=5 * (len(lines) - 1 - i))
        store.emit_log(service, parsed["level"], parsed["message"],
                       run_id=run_id, timestamp=ts,
                       exception_type=parsed["exception_type"])
    metrics = scenario.get("metrics") or {}
    error_rate = float(metrics.get("error_rate") or 0.0)
    errors = int(round(error_rate / 100.0 * REQUESTS_PER_RUN))
    errors = max(0, min(REQUESTS_PER_RUN, errors))
    store.emit_requests(service, run_id, REQUESTS_PER_RUN, errors,
                        float(metrics.get("latency_ms") or 0.0),
                        float(metrics.get("latency_baseline") or 0.0))
    if metrics.get("cpu") is not None:
        store.set_cpu(service, run_id, float(metrics["cpu"]))
    return {"log_lines": len(lines), "requests": REQUESTS_PER_RUN}
