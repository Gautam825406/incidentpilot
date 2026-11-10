import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import Landing from "./Landing";

type Any = Record<string, unknown>;

function statusBadge(s: string) {
  const cls = s === "resolved" ? "green" : s === "awaiting_approval" || s === "approved" ? "amber" : s === "abstained" ? "red" : "blue";
  return <span className={`badge ${cls}`}>{s.replaceAll("_", " ")}</span>;
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
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<"landing" | "app">("landing");
  const [integrations, setIntegrations] = useState<Any | null>(null);
  const [intLoading, setIntLoading] = useState(false);
  const [intError, setIntError] = useState("");
  const [liveMetrics, setLiveMetrics] = useState<Record<string, LiveMetrics>>({});
  const [metricsLoading, setMetricsLoading] = useState(false);
  const metricsFetchedRef = useRef<Set<string>>(new Set());

  const load = async () => {
    try {
      setHealth(await api.health());
      setIncidents((await api.incidents()).incidents);
      setScenarios((await api.scenarios()).scenarios);
      setEvalRuns((await api.evalResults()).runs);
    } catch (e) {
      setMsg(`Backend not reachable: ${(e as Error).message}. Start backend on ${"VITE_API_URL"}.`);
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

  const open = async (id: string) => setSelected(await api.incident(id));
  const login = async () => {
    try {
      const r = await api.login(email, password);
      localStorage.setItem("ip_token", r.token);
      setUser(r.user);
      setMsg(`Logged in as ${r.user.email} (${r.user.role})`);
    } catch (e) { setMsg(`Login failed: ${(e as Error).message}`); }
  };
  const simulate = async () => {
    setBusy(true); setMsg("");
    try {
      const r = await api.simulate(scenario);
      setMsg(`Investigation finished: ${r.investigation?.status}`);
      await load();
      await open(r.incident_id);
    } catch (e) { setMsg(`Simulate failed: ${(e as Error).message}`); }
    setBusy(false);
  };
  const act = async (fn: () => Promise<unknown>, label: string) => {
    setBusy(true);
    try { await fn(); setMsg(label); if (selected) await open(selected.id as string); await load(); }
    catch (e) { setMsg(`${label} failed: ${(e as Error).message}`); }
    setBusy(false);
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
        {health && <span className={`badge ${health.demo_mode ? "amber" : "green"}`}>{health.demo_mode ? "Demo mode — add GROQ_API_KEY for live LLM" : `Live LLM: ${health.model}`}</span>}
        <span style={{ flex: 1 }} />
        <button onClick={() => setView("landing")}>← Home</button>
        <button onClick={() => setTab("incidents")}>Incidents</button>
        <button onClick={() => setTab("eval")}>Eval dashboard</button>
        <button onClick={() => setTab("knowledge")}>Knowledge base</button>
      </header>
      <main>
        {msg && <div className="card"><strong>{msg}</strong></div>}
        {!user && (
          <div className="card">
            <h2>Log in to approve &amp; execute</h2>
            <p className="muted">Demo: admin@demo.local / admin123 · responder@demo.local / responder123 · viewer@demo.local / viewer123. Reads work without login.</p>
            <div className="row"><input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email" /><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password" /><button className="primary" onClick={login}>Log in</button></div>
          </div>
        )}
        {tab === "incidents" && (
          <>
            <div className="card">
              <h2>Simulate an alert</h2>
              <div className="row">
                <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
                  {scenarios.map((s) => <option key={s.key as string} value={s.key as string}>{s.title as string} ({s.service as string})</option>)}
                </select>
                <button className="primary" disabled={busy} onClick={simulate}>{busy ? "Investigating…" : "🔥 Fire alert & investigate"}</button>
              </div>
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
                    <div style={{ overflowX: "auto" }}>
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
                                <td>{i.severity as string}</td>
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
                    <p>{statusBadge(selected.status as string)} <span className="muted">{selected.service as string} · tokens {(selected.total_tokens as number) ?? 0} · cost ${(selected.total_cost_usd as number) ?? 0}</span></p>
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
                    <h4>Hypotheses</h4>
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
                          <button className="primary" disabled={busy || plan.status !== "pending"} onClick={() => act(() => api.approve(plan.id as string), "Plan approved — execution is now allowed")}>Approve plan</button>
                          <button className="danger" disabled={busy || plan.status !== "pending"} onClick={() => act(() => api.reject(plan.id as string, "Not the right fix"), "Plan rejected")}>Reject</button>
                          <button disabled={busy || plan.status !== "approved"} onClick={() => act(() => api.execute(plan.id as string, true), "Dry-run complete — nothing changed")}>Dry-run</button>
                          <button className="primary" disabled={busy || plan.status !== "approved"} onClick={() => act(() => api.execute(plan.id as string, false), "Plan executed")}>Execute (allow-list only)</button>
                          <button disabled={busy} onClick={() => act(() => api.verify(selected.id as string), "Verification checked")}>Verify recovery</button>
                        </div>
                      </>
                    ) : null; })()}
                    <h4>Timeline / audit</h4>
                    <div className="timeline">{(selected.events as Any[]).map((e) => <div key={e.id as string}><span className="pill">{e.type as string}</span> <strong>{e.actor as string}</strong> — {(e.payload as Any)?.message as string || JSON.stringify(e.payload)}</div>)}</div>
                    {selected.postmortem && <><h4>Postmortem draft</h4><div className="mono">{selected.postmortem as string}</div></>}
                    <h4>Agent runs — agent by agent</h4>
                    {((selected.agent_runs as Any[]) ?? []).length === 0 ? (
                      <p className="muted">No agent runs recorded for this incident yet.</p>
                    ) : (
                      <table><thead><tr><th>Agent</th><th>Model</th><th>Tokens</th><th>Latency</th><th>Cost</th></tr></thead><tbody>
                        {(selected.agent_runs as Any[]).map((r, i) => <tr key={i}><td>{r.agent as string}</td><td>{r.model as string}</td><td>{r.tokens as number}</td><td>{r.latency_ms as number} ms</td><td>${(r.cost_usd as number) ?? 0}</td></tr>)}
                      </tbody></table>
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
            <button className="primary" disabled={busy} onClick={() => act(() => api.runEval(), "Eval run complete — see results below")}>Run eval harness (admin)</button>
            {evalRuns.map((r) => (
              <div key={r.id as string} className="evidence">
                <strong>{r.dataset_version as string}</strong> · {r.model as string} · {r.run_at as string}
                <div className="mono">{JSON.stringify(r.metrics, null, 2)}</div>
              </div>
            ))}
            {evalRuns.length === 0 && <p className="muted">No eval runs yet. Run the harness, or run <code>python -m evals.run_eval</code> in backend/.</p>}
          </div>
        )}
        {tab === "knowledge" && (
          <div className="card">
            <h2>Knowledge base — runbooks &amp; postmortems</h2>
            <p className="muted">Hybrid RAG (keyword + vector) over the seeded runbooks and postmortems. Published postmortems feed back here — the learning loop.</p>
            <button onClick={async () => { const d = await api.documents(); setMsg(JSON.stringify(d.documents.map((x: Any) => x.title))); }}>List documents</button>
          </div>
        )}
        <p className="muted">IncidentPilot v1 · Simulated services only · No Slack/Jira in v1 · What it will not do: no unapproved execution, no deletions, no production DB writes.</p>
      </main>
      </>
      )}
    </>
  );
}
