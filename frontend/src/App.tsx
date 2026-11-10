import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import Landing from "./Landing";

type Any = Record<string, unknown>;

type Notice = { text: string; kind: "info" | "error" | "success" } | null;

function statusBadge(s: string) {
  const cls =
    s === "resolved" ? "green"
    : s === "awaiting_approval" || s === "approved" ? "amber"
    : s === "abstained" || s === "rejected" || s === "failed" ? "red"
    : "blue";
  return <span className={`badge ${cls}`}>{s.replaceAll("_", " ")}</span>;
}

function severityBadge(s: string) {
  const cls = s === "critical" || s === "high" ? "red" : s === "medium" ? "amber" : "blue";
  return <span className={`badge ${cls}`}>{s}</span>;
}

type LiveMetrics = { errorRate?: string; cpu?: string; memory?: string; latency?: string };

/** Extract error_rate / cpu / memory / latency from one metric-evidence content string.
 *  Handles the backend's plain-text form ("error_rate 18.7% (baseline 0.8%), latency
 *  245ms (baseline 120ms), cpu 92%") and a JSON object form. Returns only fields
 *  actually present — never invents a value. */
function parseMetricContent(content: string): LiveMetrics {
  const out: LiveMetrics = {};
  const trimmed = content.trim();
  if (trimmed.startsWith("{")) {
    try {
      const j = JSON.parse(trimmed) as Record<string, unknown>;
      if (typeof j.error_rate === "number") out.errorRate = `${j.error_rate}%`;
      if (typeof j.cpu === "number") out.cpu = `${j.cpu}%`;
      if (typeof j.memory === "number") out.memory = `${j.memory}%`;
      if (typeof j.latency_ms === "number") out.latency = `${j.latency_ms} ms`;
      else if (typeof j.latency === "number") out.latency = `${j.latency} ms`;
      return out;
    } catch { /* not JSON — fall through to regex parsing */ }
  }
  const pick = (re: RegExp) => { const m = trimmed.match(re); return m ? m[1] : undefined; };
  const er = pick(/error_rate\s*[:=]?\s*([\d.]+)\s*%/i);
  if (er) out.errorRate = `${er}%`;
  const cpu = pick(/(?:^|[\s,])cpu\s*[:=]?\s*([\d.]+)\s*%/i);
  if (cpu) out.cpu = `${cpu}%`;
  const mem = pick(/memory\s*[:=]?\s*([\d.]+)\s*%/i);
  if (mem) out.memory = `${mem}%`;
  const lat = pick(/latency\s*[:=]?\s*([\d.]+)\s*ms/i);
  if (lat) out.latency = `${lat} ms`;
  return out;
}

/** Merge metric evidence (kind === "metric") from an incident detail into one
 *  LiveMetrics record. Fields with no real evidence stay absent ("—" in the UI). */
function metricsFromEvidence(evidence: Any[]): LiveMetrics {
  const merged: LiveMetrics = {};
  for (const e of evidence ?? []) {
    if (e.kind !== "metric") continue;
    const p = parseMetricContent(String(e.content ?? ""));
    if (p.errorRate && !merged.errorRate) merged.errorRate = p.errorRate;
    if (p.cpu && !merged.cpu) merged.cpu = p.cpu;
    if (p.memory && !merged.memory) merged.memory = p.memory;
    if (p.latency && !merged.latency) merged.latency = p.latency;
  }
  return merged;
}

/** One evidence group (Logs / Metrics / Knowledge). Renders nothing when empty.
 *  Shows the source ref and a timestamp only when the backend supplied one. */
function EvidenceGroup({ label, items }: { label: string; items: Any[] }) {
  if (items.length === 0) return null;
  return (
    <>
      <h4>{label} ({items.length})</h4>
      {items.map((e) => (
        <div key={e.id as string} className="evidence">
          <strong>{e.source_ref as string}</strong>
          {typeof e.created_at === "string" && e.created_at ? <span className="muted"> · {e.created_at}</span> : null}
          <div className="mono">{e.content as string}</div>
        </div>
      ))}
    </>
  );
}

/** Latency per eval case, drawn only from the run's real `metrics.per_case`
 *  array (scenario + latency_ms). Renders nothing when the run has no per-case data. */
function LatencyChart({ perCase }: { perCase: Any[] }) {
  const cases = perCase.filter((c) => typeof c.latency_ms === "number");
  if (cases.length === 0) return null;
  const max = Math.max(...cases.map((c) => c.latency_ms as number), 1);
  return (
    <div className="bar-chart" role="img" aria-label={`Eval latency per case, ${cases.length} cases`}>
      {cases.map((c) => (
        <div key={c.scenario as string} className="bar-row">
          <span className="bar-label">{c.scenario as string}</span>
          <div className="bar-track">
            <div
              className={`bar-fill ${c.correct_top1 === false ? "miss" : ""}`}
              style={{ width: `${Math.max(4, ((c.latency_ms as number) / max) * 100)}%` }}
            />
          </div>
          <span className="bar-value">{c.latency_ms as number} ms</span>
          <span className={`bar-result ${c.correct_top1 === false ? "bad" : "ok"}`} title={c.correct_top1 === false ? "Top-1 root cause wrong" : "Top-1 root cause correct"}>
            {c.correct_top1 === false ? "✗" : "✓"}
          </span>
        </div>
      ))}
    </div>
  );
}

/** Readable summary of one stored eval run — stat cards + per-case latency chart,
 *  all from the run's real stored metrics. Fields the run does not have are omitted. */
function EvalRunCard({ run }: { run: Any }) {
  const m = (run.metrics ?? {}) as Any;
  const perCase = Array.isArray(m.per_case) ? (m.per_case as Any[]) : [];
  const demo = m.demo_mode === true;
  const stats: { label: string; value: string }[] = [];
  if (typeof m.correct_top1 === "number" && typeof m.n === "number") {
    stats.push({ label: "Exact top-1 root cause", value: `${m.correct_top1}/${m.n}` });
  }
  if (typeof m.semantic_correct_top1 === "number" && typeof m.semantic_top1_total === "number") {
    stats.push({ label: "Semantic top-1 (primary)", value: `${m.semantic_correct_top1}/${m.semantic_top1_total}` });
  }
  if (typeof m.abstention_correct === "string") {
    stats.push({ label: "Abstention correct", value: m.abstention_correct as string });
  }
  if (typeof m.evidence_coverage === "number") {
    stats.push({ label: "Evidence coverage", value: `${Math.round((m.evidence_coverage as number) * 100)}%` });
  }
  if (typeof m.unsafe_actions === "number") {
    stats.push({ label: "Unsafe actions", value: String(m.unsafe_actions) });
  }
  if (typeof m.median_latency_ms === "number") {
    stats.push({ label: "Median latency", value: `${m.median_latency_ms} ms` });
  }
  if (typeof m.total_tokens === "number") {
    stats.push({ label: "Total tokens", value: String(m.total_tokens) });
  }
  if (typeof m.total_cost_usd === "number") {
    stats.push({ label: "Total cost", value: `$${m.total_cost_usd}` });
  }
  return (
    <div className="evidence">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong>{run.dataset_version as string}</strong>
        <span className={`badge ${demo ? "amber" : "green"}`}>{demo ? "Demo-mode run" : "Live-LLM run"}</span>
      </div>
      <div className="muted" style={{ marginTop: 4 }}>
        Model: {run.model as string}{typeof run.run_at === "string" && run.run_at ? ` · ${run.run_at}` : ""}
        {demo ? " · demo-model numbers, not live-LLM results" : ""}
      </div>
      {stats.length > 0 && (
        <div className="stat-grid">
          {stats.map((s) => (
            <div key={s.label} className="stat-card"><strong>{s.value}</strong><span>{s.label}</span></div>
          ))}
        </div>
      )}
      {perCase.length > 0 && (
        <>
          <h4 style={{ marginTop: 16 }}>Latency per case</h4>
          <LatencyChart perCase={perCase} />
        </>
      )}
      {perCase.length > 0 && (
        <div className="table-scroll">
          <table className="mini-table">
            <thead><tr><th>Case</th><th>Status</th><th>Confidence</th><th>Top-1</th><th>Latency</th></tr></thead>
            <tbody>
              {perCase.map((c) => (
                <tr key={c.scenario as string}>
                  <td>{c.scenario as string}</td>
                  <td>{typeof c.status === "string" ? statusBadge(c.status as string) : "—"}</td>
                  <td>{typeof c.confidence === "number" ? `${Math.round((c.confidence as number) * 100)}%` : "—"}</td>
                  <td>{c.correct_top1 === true ? "✓ correct" : c.correct_top1 === false ? "✗ wrong" : "—"}</td>
                  <td>{typeof c.latency_ms === "number" ? `${c.latency_ms} ms` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [health, setHealth] = useState<Any | null>(null);
  const [incidents, setIncidents] = useState<Any[]>([]);
  const [selected, setSelected] = useState<Any | null>(null);
  const [scenarios, setScenarios] = useState<Any[]>([]);
  const [scenario, setScenario] = useState("checkout-oom");
  const [evalRuns, setEvalRuns] = useState<Any[]>([]);
  const [tab, setTab] = useState<"incidents" | "eval" | "knowledge">("incidents");
  const [email, setEmail] = useState("admin@demo.local");
  const [password, setPassword] = useState("admin123");
  const [user, setUser] = useState<Any | null>(null);
  const [loginBusy, setLoginBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<"landing" | "app">("landing");
  const [integrations, setIntegrations] = useState<Any | null>(null);
  const [intLoading, setIntLoading] = useState(false);
  const [intError, setIntError] = useState("");
  const [notifyBusy, setNotifyBusy] = useState<"jira" | "slack" | null>(null);
  const [liveMetrics, setLiveMetrics] = useState<Record<string, LiveMetrics>>({});
  const [metricsLoading, setMetricsLoading] = useState(false);
  const metricsFetchedRef = useRef<Set<string>>(new Set());
  // First-load boot state: the free Render backend can take 50+ s to wake up,
  // so the console shows a "waking up" state instead of empty lists until the
  // first load succeeds or fails. Later reloads never re-enter this state.
  const [bootState, setBootState] = useState<"loading" | "ready" | "error">("loading");
  const [bootError, setBootError] = useState("");
  const firstLoadRef = useRef(true);
  // Knowledge base documents, loaded when the tab is opened.
  const [docs, setDocs] = useState<Any[]>([]);
  const [docsLoading, setDocsLoading] = useState(false);
  const [docsError, setDocsError] = useState("");

  const flash = (text: string, kind: "info" | "error" | "success" = "info") =>
    setNotice({ text, kind });

  const load = async () => {
    const first = firstLoadRef.current;
    try {
      setHealth(await api.health());
      setIncidents((await api.incidents()).incidents);
      setScenarios((await api.scenarios()).scenarios);
      setEvalRuns((await api.evalResults()).runs);
      if (first) { firstLoadRef.current = false; setBootState("ready"); }
    } catch (e) {
      const detail = `Backend not reachable: ${(e as Error).message}. Check VITE_API_URL — the deployed backend is https://incidentpilot-backend-lmi9.onrender.com.`;
      if (first) { firstLoadRef.current = false; setBootState("error"); setBootError(detail); }
      else flash(detail, "error");
    }
  };
  useEffect(() => { load(); }, []);

  const loadIntegrations = async () => {
    setIntLoading(true); setIntError("");
    try {
      setIntegrations(await api.integrationsStatus());
    } catch {
      setIntegrations(null);
      setIntError("Could not reach backend — integrations status unavailable");
    }
    setIntLoading(false);
  };
  // Fetch integrations once on entering the console (and on manual Refresh).
  useEffect(() => { if (view === "app") loadIntegrations(); }, [view]);

  const loadDocs = async () => {
    setDocsLoading(true); setDocsError("");
    try {
      setDocs((await api.documents()).documents);
    } catch (e) {
      setDocs([]);
      setDocsError(`Could not load documents: ${(e as Error).message}`);
    }
    setDocsLoading(false);
  };
  // Load the knowledge base when the tab is opened.
  useEffect(() => { if (tab === "knowledge") loadDocs(); }, [tab]);

  // Live metrics for the dashboard come only from each active incident's
  // metric evidence — fetched once per incident, never invented.
  useEffect(() => {
    const active = incidents.filter((i) => (i.status as string) !== "resolved");
    const missing = active.filter((i) => !metricsFetchedRef.current.has(i.id as string));
    if (missing.length === 0) return;
    let cancelled = false;
    setMetricsLoading(true);
    (async () => {
      const next: Record<string, LiveMetrics> = {};
      for (const i of missing) {
        metricsFetchedRef.current.add(i.id as string);
        try {
          const d = await api.incident(i.id as string);
          next[i.id as string] = metricsFromEvidence((d.evidence as Any[]) ?? []);
        } catch {
          next[i.id as string] = {};
        }
      }
      if (!cancelled) { setLiveMetrics((prev) => ({ ...prev, ...next })); setMetricsLoading(false); }
    })();
    return () => { cancelled = true; };
  }, [incidents]);

  const open = async (id: string) => {
    try {
      setSelected(await api.incident(id));
    } catch (e) {
      flash(`Could not open incident: ${(e as Error).message}`, "error");
    }
  };
  const login = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!email.trim() || !password) {
      flash("Enter your email and password to log in.", "error");
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())) {
      flash("That email address doesn't look valid.", "error");
      return;
    }
    setLoginBusy(true);
    try {
      const r = await api.login(email.trim(), password);
      localStorage.setItem("ip_token", r.token);
      setUser(r.user);
      flash(`Logged in as ${r.user.email} (${r.user.role})`, "success");
    } catch (err) { flash(`Login failed: ${(err as Error).message}`, "error"); }
    setLoginBusy(false);
  };
  const simulate = async () => {
    if (!scenario) { flash("Pick a scenario first.", "error"); return; }
    setBusy(true); setNotice(null);
    try {
      const r = await api.simulate(scenario);
      flash(`Investigation finished: ${r.investigation?.status}`, "success");
      await load();
      await open(r.incident_id);
    } catch (e) { flash(`Simulate failed: ${(e as Error).message}`, "error"); }
    setBusy(false);
  };
  const act = async (fn: () => Promise<unknown>, label: string) => {
    setBusy(true);
    try { await fn(); flash(label, "success"); if (selected) await open(selected.id as string); await load(); }
    catch (e) { flash(`${label} failed: ${(e as Error).message}`, "error"); }
    setBusy(false);
  };
  const notifyChannel = async (channel: "jira" | "slack") => {
    if (!selected) return;
    setNotifyBusy(channel); setNotice(null);
    try {
      const r = await api.notify(selected.id as string, channel);
      if (channel === "jira") {
        flash(r.already_linked ? `Jira issue already linked: ${r.issue_key}` : `Jira issue created: ${r.issue_key}`, "success");
      } else {
        flash("Slack notification sent", "success");
      }
      await open(selected.id as string); await load();
    } catch (e) { flash(`Notify via ${channel} failed: ${(e as Error).message}`, "error"); }
    setNotifyBusy(null);
  };

  return (
    <>
      {view === "landing" ? (
        <Landing health={health} onEnter={() => setView("app")} />
      ) : (
      <>
      <header>
        <h1>🚨 IncidentPilot</h1>
        <span className="muted">Multi-agent incident resolver — LangGraph + Groq</span>
        {health
          ? <span className={`badge ${health.demo_mode ? "amber" : "green"}`}>{health.demo_mode ? "Demo mode — add GROQ_API_KEY for live LLM" : `Live LLM: ${health.model}`}</span>
          : <span className="badge blue">Connecting to backend…</span>}
        <span style={{ flex: 1 }} />
        <button onClick={() => setView("landing")}>← Home</button>
        <button onClick={() => setTab("incidents")} aria-current={tab === "incidents" ? "page" : undefined}>Incidents</button>
        <button onClick={() => setTab("eval")} aria-current={tab === "eval" ? "page" : undefined}>Eval dashboard</button>
        <button onClick={() => setTab("knowledge")} aria-current={tab === "knowledge" ? "page" : undefined}>Knowledge base</button>
      </header>
      <main>
        {notice && (
          <div className={`notice ${notice.kind}`} role="status" aria-live="polite">
            <strong>{notice.text}</strong>
            <button className="notice-close" onClick={() => setNotice(null)} aria-label="Dismiss message">✕</button>
          </div>
        )}
        {bootState === "loading" && (
          <div className="card boot">
            <div className="spinner" aria-hidden="true" />
            <h2>Waking up the backend…</h2>
            <p className="muted">
              The backend runs on Render's free tier and can take 50+ seconds to wake up on the
              first request after being idle. The console will appear as soon as it answers —
              no need to refresh.
            </p>
          </div>
        )}
        {bootState === "error" && (
          <div className="card">
            <h2>Backend not reachable</h2>
            <p className="muted">{bootError}</p>
            <p className="muted">
              If the backend is on Render's free tier it may still be waking up — wait a minute
              and retry.
            </p>
            <button
              className="primary"
              onClick={() => { firstLoadRef.current = true; setBootState("loading"); setBootError(""); load(); }}
            >
              ↻ Retry connection
            </button>
          </div>
        )}
        {bootState === "ready" && (
        <>
        {!user && (
          <div className="card">
            <h2>Log in to approve &amp; execute</h2>
            <p className="muted">Demo: admin@demo.local / admin123 · responder@demo.local / responder123 · viewer@demo.local / viewer123. Reads work without login.</p>
            <form onSubmit={login} className="login-form">
              <div className="field">
                <label htmlFor="login-email">Email</label>
                <input id="login-email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="admin@demo.local" required />
              </div>
              <div className="field">
                <label htmlFor="login-password">Password</label>
                <input id="login-password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password" required />
              </div>
              <button className="primary" type="submit" disabled={loginBusy}>{loginBusy ? "Logging in…" : "Log in"}</button>
            </form>
          </div>
        )}
        {user && (
          <p className="muted">Signed in as <strong>{user.email as string}</strong> ({user.role as string}) — you can approve, execute and notify.</p>
        )}
        {tab === "incidents" && (
          <>
            <div className="card">
              <h2>Simulate an alert</h2>
              <div className="row">
                <div className="field grow">
                  <label htmlFor="scenario-select">Scenario</label>
                  <select id="scenario-select" value={scenario} onChange={(e) => setScenario(e.target.value)} disabled={scenarios.length === 0}>
                    {scenarios.map((s) => <option key={s.key as string} value={s.key as string}>{s.title as string} ({s.service as string})</option>)}
                  </select>
                </div>
                <button className="primary" disabled={busy || scenarios.length === 0} onClick={simulate} title={scenarios.length === 0 ? "Scenarios are still loading" : "Fire the alert and run the investigation"}>
                  {busy ? "Investigating…" : "🔥 Fire alert & investigate"}
                </button>
              </div>
              {scenarios.length === 0 && <p className="muted">Loading scenarios…</p>}
              <p className="muted">Alert → LangGraph supervisor (Triage · Logs · Metrics · Knowledge) → evidence-linked hypotheses → plan → your approval → guarded execution → verification → postmortem. No real production action is ever taken — simulated services only.</p>
            </div>
            <div className="card">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h2 style={{ margin: 0 }}>Monitoring Connections</h2>
                <button disabled={intLoading} onClick={loadIntegrations}>{intLoading ? "Refreshing…" : "↻ Refresh"}</button>
              </div>
              {intLoading && !integrations && <p className="muted">Checking integrations…</p>}
              {intError && <p className="muted">⚠️ {intError}</p>}
              {integrations && (() => {
                const prom = (integrations.prometheus ?? {}) as Any;
                const logs = (integrations.logs ?? {}) as Any;
                const graf = (integrations.grafana ?? {}) as Any;
                const promStatus = !prom.configured ? "Not connected" : prom.reachable === true ? "Connected" : prom.reachable === false ? "Unreachable" : "Configured — reachability unknown";
                const promCls = !prom.configured ? "red" : prom.reachable === true ? "green" : prom.reachable === false ? "red" : "amber";
                const logsProvider = typeof logs.provider === "string" && logs.provider !== "none" && logs.provider !== "" ? (logs.provider as string) : null;
                const grafUrl = typeof graf.url === "string" && graf.url ? (graf.url as string) : null;
                const jira = (integrations.jira ?? {}) as Any;
                const slack = (integrations.slack ?? {}) as Any;
                const jiraUrl = typeof jira.url === "string" && jira.url ? (jira.url as string) : null;
                const jiraProject = typeof jira.project === "string" && jira.project ? (jira.project as string) : null;
                const slackChannel = typeof slack.channel === "string" && slack.channel ? (slack.channel as string) : null;
                return (
                  <div className="conn-grid">
                    <div className="conn-card">
                      <strong>Prometheus</strong>
                      <span className={`badge ${promCls}`}>{promStatus}</span>
                      <div className="muted">{typeof prom.url === "string" && prom.url ? prom.url : "No Prometheus URL configured"}</div>
                    </div>
                    <div className="conn-card">
                      <strong>Logs</strong>
                      {logsProvider ? <span className="badge blue">{logsProvider}</span> : <span className="badge red">Not connected</span>}
                      <div className="muted">{logsProvider ? `Provider: ${logsProvider}` : "No logs provider configured"}</div>
                    </div>
                    <div className="conn-card">
                      <strong>Grafana</strong>
                      {graf.configured ? <span className="badge green">Configured</span> : <span className="badge red">Not connected</span>}
                      <div className="muted">{graf.configured && grafUrl ? <a href={grafUrl} target="_blank" rel="noreferrer">Open Grafana ↗</a> : "No Grafana URL configured"}</div>
                    </div>
                    <div className="conn-card">
                      <strong>Jira</strong>
                      {jira.configured ? <span className="badge green">Configured</span> : <span className="badge red">Not connected</span>}
                      <div className="muted">{jira.configured ? `${jiraUrl ?? ""}${jiraProject ? ` · project ${jiraProject}` : ""}` : "No Jira configured — set JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY"}</div>
                    </div>
                    <div className="conn-card">
                      <strong>Slack</strong>
                      {slack.configured ? <span className="badge green">Configured</span> : <span className="badge red">Not connected</span>}
                      <div className="muted">{slack.configured ? `Channel: ${slackChannel ?? "(webhook default)"}` : "No Slack webhook configured — set SLACK_WEBHOOK_URL"}</div>
                    </div>
                  </div>
                );
              })()}
              {!integrations && !intLoading && !intError && <p className="muted">Integrations status not loaded yet.</p>}
            </div>
            <div className="card">
              <h2>Live Incident Dashboard</h2>
              <p className="muted">Active incidents and the live metrics the investigation actually observed. Values come only from metric evidence — “—” / “No live metrics” means no metric was recorded, never an invented number.</p>
              {(() => {
                const active = incidents.filter((i) => (i.status as string) !== "resolved");
                if (active.length === 0) return <p className="muted">No active incidents — everything is resolved.</p>;
                const hasAnyMetrics = active.some((i) => {
                  const m = liveMetrics[i.id as string] ?? {};
                  return Boolean(m.errorRate || m.cpu || m.memory || m.latency);
                });
                const cell = (v: string | undefined) => (v ? v : "—");
                return (
                  <>
                    <div className="table-scroll">
                      <table className="live-table">
                        <thead><tr><th>Incident</th><th>Service</th><th>Severity</th><th>Status</th><th>Error rate</th><th>CPU</th><th>Memory</th><th>Latency</th></tr></thead>
                        <tbody>
                          {active.map((i) => {
                            const m = liveMetrics[i.id as string] ?? {};
                            const noMetrics = !m.errorRate && !m.cpu && !m.memory && !m.latency;
                            return (
                              <tr key={i.id as string}>
                                <td><button onClick={() => open(i.id as string)}>{i.title as string}</button>{noMetrics && <div className="muted">No live metrics</div>}</td>
                                <td>{i.service as string}</td>
                                <td>{severityBadge(i.severity as string)}</td>
                                <td>{statusBadge(i.status as string)}</td>
                                <td>{cell(m.errorRate)}</td>
                                <td>{cell(m.cpu)}</td>
                                <td>{cell(m.memory)}</td>
                                <td>{cell(m.latency)}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                    {metricsLoading && <p className="muted">Loading live metrics from incident evidence…</p>}
                    {!metricsLoading && !hasAnyMetrics && <p className="muted">No live metrics recorded for any active incident yet.</p>}
                  </>
                );
              })()}
            </div>
            <div className="grid">
              <div className="card">
                <h2>Incidents ({incidents.length})</h2>
                {incidents.map((i) => (
                  <div key={i.id as string} className="row" style={{ justifyContent: "space-between", padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
                    <div><strong>{i.title as string}</strong><div className="muted">{i.service as string} · {i.severity as string}</div></div>
                    <span className="row">{statusBadge(i.status as string)}<button onClick={() => open(i.id as string)}>Open</button></span>
                  </div>
                ))}
                {incidents.length === 0 && <p className="muted">No incidents yet — fire an alert above.</p>}
              </div>
              <div className="card">
                <h2>Incident detail</h2>
                {!selected && <p className="muted">Select an incident to see its timeline, evidence, hypotheses and plan.</p>}
                {selected && (
                  <>
                    <h3>{selected.title as string}</h3>
                    <p>{statusBadge(selected.status as string)} <span className="muted">{selected.service as string} · {selected.severity as string} · tokens {(selected.total_tokens as number) ?? 0} · cost ${(selected.total_cost_usd as number) ?? 0}</span></p>
                    {(() => {
                      const mon = selected.monitoring as Any | undefined;
                      const dash = mon && typeof mon.grafana_dashboard_url === "string" && mon.grafana_dashboard_url ? (mon.grafana_dashboard_url as string) : null;
                      return (
                        <div className="evidence">
                          <strong>Monitoring</strong>
                          <div style={{ marginTop: 6 }}>
                            {dash ? <a href={dash} target="_blank" rel="noreferrer">Open Grafana dashboard ↗</a> : <span className="muted">No dashboard linked</span>}
                          </div>
                          {mon && (
                            <div className="muted" style={{ marginTop: 4 }}>
                              Prometheus: {mon.prometheus_configured ? "configured" : "not connected"} · Logs provider: {typeof mon.logs_provider === "string" && mon.logs_provider ? (mon.logs_provider as string) : "not connected"}
                            </div>
                          )}
                          <div className="muted" style={{ marginTop: 4 }}>Dashboard links are links only — Grafana does not supply the metrics shown in this console.</div>
                        </div>
                      );
                    })()}
                    {(() => {
                      const jiraConfigured = Boolean((((integrations as Any)?.jira ?? {}) as Any).configured);
                      const slackConfigured = Boolean((((integrations as Any)?.slack ?? {}) as Any).configured);
                      const linkedKey = typeof selected.jira_issue_key === "string" && selected.jira_issue_key ? (selected.jira_issue_key as string) : null;
                      const linkedUrl = typeof selected.jira_issue_url === "string" && selected.jira_issue_url ? (selected.jira_issue_url as string) : null;
                      return (
                        <div className="evidence">
                          <strong>Notifications — Jira / Slack</strong>
                          {linkedKey && <div style={{ marginTop: 6 }}>Jira issue: {linkedUrl ? <a href={linkedUrl} target="_blank" rel="noreferrer">{linkedKey} ↗</a> : linkedKey}</div>}
                          <div className="row" style={{ marginTop: 8 }}>
                            <button disabled={busy || notifyBusy !== null || !user || !jiraConfigured || Boolean(linkedKey)} onClick={() => notifyChannel("jira")} title={!user ? "Log in as admin/responder to notify" : !jiraConfigured ? "Jira is not configured in the backend" : linkedKey ? "A Jira issue is already linked to this incident" : "Create a Jira issue for this incident"}>
                              {notifyBusy === "jira" ? "Creating…" : linkedKey ? "Jira issue linked ✓" : "Create Jira issue"}
                            </button>
                            <button disabled={busy || notifyBusy !== null || !user || !slackConfigured} onClick={() => notifyChannel("slack")} title={!user ? "Log in as admin/responder to notify" : !slackConfigured ? "Slack is not configured in the backend" : "Send a notification to the configured Slack channel"}>
                              {notifyBusy === "slack" ? "Sending…" : "Notify Slack"}
                            </button>
                          </div>
                          <div className="muted" style={{ marginTop: 4 }}>
                            {!user ? "Log in as admin/responder to notify." : !jiraConfigured && !slackConfigured ? "Jira and Slack are not configured — set them in the backend .env to enable notifications." : !jiraConfigured ? "Jira not configured." : !slackConfigured ? "Slack not configured." : "Notifications go to the configured Jira project / Slack channel."}
                          </div>
                        </div>
                      );
                    })()}
                    <h4>Hypotheses</h4>
                    {(selected.hypotheses as Any[]).length === 0 && <p className="muted">No hypotheses yet — run the investigation.</p>}
                    {(selected.hypotheses as Any[]).map((h) => (
                      <div key={h.id as string} className="evidence"><span className="conf">#{h.rank as number} — {Math.round((h.confidence as number) * 100)}% confidence</span><br />{h.statement as string}<br /><span className="muted">Evidence: {(h.evidence_ids as string[]).length} linked items · {h.status as string}</span></div>
                    ))}
                    <h4>Evidence</h4>
                    {(() => {
                      const evs = ((selected.evidence as Any[]) ?? []);
                      if (evs.length === 0) return <p className="muted">No evidence recorded for this incident.</p>;
                      return (
                        <>
                          <EvidenceGroup label="Logs evidence" items={evs.filter((e) => e.kind === "log")} />
                          <EvidenceGroup label="Metrics evidence" items={evs.filter((e) => e.kind === "metric")} />
                          <EvidenceGroup label="Knowledge references" items={evs.filter((e) => e.kind === "doc")} />
                          <EvidenceGroup label="Other evidence" items={evs.filter((e) => e.kind !== "log" && e.kind !== "metric" && e.kind !== "doc")} />
                        </>
                      );
                    })()}
                    {(() => { const plan = selected.plan as Any | null; return plan ? (
                      <>
                        <h4>Remediation plan — {plan.status as string}</h4>
                        {(plan.steps as Any[]).map((s, i) => <div key={i}>• <code>{s.tool as string}</code> — {s.description as string} <span className="muted">(risk: {s.risk as string})</span></div>)}
                        {typeof plan.risk === "string" && plan.risk ? <p className="muted">Plan risk: {plan.risk}</p> : null}
                        <p className="muted">Rollback: {plan.rollback as string}</p>
                        <p className="muted">Execution status: {plan.status as string}{plan.approved_by ? ` · approved by ${plan.approved_by as string}` : ""}{plan.expires_at ? ` · approval expires ${plan.expires_at as string}` : ""}</p>
                        <p className="muted">
                          Approval is a one-time token: it expires 30 minutes after approval and every
                          step is idempotent. Dry-run walks the same steps without changing anything —
                          use it before executing.
                        </p>
                        <p className="muted">
                          {plan.status === "pending"
                            ? "Plan is pending — approve it to allow execution, or reject it."
                            : plan.status === "approved"
                              ? "Plan is approved — dry-run it first, then execute."
                              : `Plan is ${plan.status} — no further approval or execution action is available.`}
                        </p>
                        {(() => {
                          const hyps = (((selected.hypotheses as Any[]) ?? []).slice()).sort((a, b) => (a.rank as number) - (b.rank as number));
                          const top = hyps[0];
                          if (!top) return null;
                          const ids = new Set((top.evidence_ids as string[]) ?? []);
                          const items = ((selected.evidence as Any[]) ?? []).filter((e) => ids.has(e.id as string));
                          return (
                            <>
                              <h4>Evidence behind the proposed action</h4>
                              <p className="muted">Top hypothesis (#{top.rank as number} — {Math.round((top.confidence as number) * 100)}%): {top.statement as string}</p>
                              {items.length === 0 ? <p className="muted">No evidence items linked to the top hypothesis.</p> : items.map((e) => (
                                <div key={e.id as string} className="evidence"><span className="pill">{e.kind as string}</span> <strong>{e.source_ref as string}</strong><div className="mono">{e.content as string}</div></div>
                              ))}
                            </>
                          );
                        })()}
                        <div className="row">
                          <button className="primary" disabled={busy || plan.status !== "pending" || !user} onClick={() => act(() => api.approve(plan.id as string), "Plan approved — execution is now allowed")} title={!user ? "Log in to approve" : plan.status !== "pending" ? `Plan is ${plan.status}, not pending` : "Approve this plan"}>Approve plan</button>
                          <button className="danger" disabled={busy || plan.status !== "pending" || !user} onClick={() => act(() => api.reject(plan.id as string, "Not the right fix"), "Plan rejected")} title={!user ? "Log in to reject" : plan.status !== "pending" ? `Plan is ${plan.status}, not pending` : "Reject this plan"}>Reject</button>
                          <button disabled={busy || plan.status !== "approved" || !user} onClick={() => act(() => api.execute(plan.id as string, true), "Dry-run complete — nothing changed")} title={!user ? "Log in to run a dry-run" : plan.status !== "approved" ? "Approve the plan first" : "Run the plan in dry-run mode — nothing changes"}>Dry-run</button>
                          <button className="primary" disabled={busy || plan.status !== "approved" || !user} onClick={() => act(() => api.execute(plan.id as string, false), "Plan executed")} title={!user ? "Log in to execute" : plan.status !== "approved" ? "Approve the plan first" : "Execute the plan (allow-list only)"}>Execute (allow-list only)</button>
                          <button disabled={busy} onClick={() => act(() => api.verify(selected.id as string), "Verification checked")}>Verify recovery</button>
                          <button disabled={busy} onClick={() => act(() => api.investigate(selected.id as string), "Investigation re-run")}>Re-run investigation</button>
                        </div>
                        {!user && <p className="muted">Log in above to approve, reject, dry-run or execute — verification and re-investigation work without login.</p>}
                      </>
                    ) : null; })()}
                    <h4>Timeline / audit</h4>
                    {(selected.events as Any[]).length === 0
                      ? <p className="muted">No timeline events recorded yet.</p>
                      : <div className="timeline">{(selected.events as Any[]).map((e) => <div key={e.id as string}><span className="pill">{e.type as string}</span> <strong>{e.actor as string}</strong> — {(e.payload as Any)?.message as string || JSON.stringify(e.payload)}</div>)}</div>}
                    {selected.postmortem && <><h4>Postmortem draft</h4><div className="mono">{selected.postmortem as string}</div></>}
                    <h4>Agent runs — agent by agent</h4>
                    {((selected.agent_runs as Any[]) ?? []).length === 0 ? (
                      <p className="muted">No agent runs recorded for this incident yet.</p>
                    ) : (
                      <div className="table-scroll">
                        <table className="mini-table"><thead><tr><th>Agent</th><th>Model</th><th>Tokens</th><th>Latency</th><th>Cost</th></tr></thead><tbody>
                          {(selected.agent_runs as Any[]).map((r, i) => <tr key={i}><td>{r.agent as string}</td><td>{r.model as string}</td><td>{r.tokens as number}</td><td>{r.latency_ms as number} ms</td><td>${(r.cost_usd as number) ?? 0}</td></tr>)}
                        </tbody></table>
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          </>
        )}
        {tab === "eval" && (
          <div className="card">
            <h2>Evaluation dashboard</h2>
            <p className="muted">Golden-set harness (6 cases). Numbers below are from real runs stored in the backend — never invented. Unsafe-action target: 0.</p>
            <button className="primary" disabled={busy || !user} onClick={() => act(() => api.runEval(), "Eval run complete — see results below")} title={!user ? "Log in as admin to run the eval harness" : "Run the golden-set eval harness (admin only)"}>Run eval harness (admin)</button>
            {!user && <p className="muted">Running the harness requires an admin login — log in above first. Stored runs are visible without login.</p>}
            {evalRuns.map((r) => (
              <EvalRunCard key={r.id as string} run={r} />
            ))}
            {evalRuns.length === 0 && <p className="muted">No eval runs yet. Run the harness, or run <code>python -m evals.run_eval</code> in backend/.</p>}
          </div>
        )}
        {tab === "knowledge" && (
          <div className="card">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h2 style={{ margin: 0 }}>Knowledge base — runbooks &amp; postmortems</h2>
              <button disabled={docsLoading} onClick={loadDocs}>{docsLoading ? "Refreshing…" : "↻ Refresh"}</button>
            </div>
            <p className="muted">Hybrid RAG (keyword + vector) over the seeded runbooks and postmortems. Published postmortems feed back here — the learning loop.</p>
            {docsLoading && docs.length === 0 && <p className="muted">Loading documents…</p>}
            {docsError && <p className="muted">⚠️ {docsError}</p>}
            {!docsLoading && !docsError && docs.length === 0 && (
              <p className="muted">No documents yet — runbooks and postmortems appear here once seeded in the backend.</p>
            )}
            {docs.length > 0 && (
              <div className="table-scroll">
                <table className="mini-table">
                  <thead><tr><th>Title</th><th>Kind</th><th>Service</th></tr></thead>
                  <tbody>
                    {docs.map((d) => (
                      <tr key={d.id as string}>
                        <td><strong>{d.title as string}</strong></td>
                        <td><span className={`badge ${d.kind === "postmortem" ? "amber" : "blue"}`}>{d.kind as string}</span></td>
                        <td>{(d.service as string) || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
        <p className="muted">IncidentPilot v1 · Simulated services only · Jira/Slack optional via backend env (see README) · What it will not do: no unapproved execution, no deletions, no production DB writes.</p>
        </>
        )}
      </main>
      </>
      )}
    </>
  );
}
