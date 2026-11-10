import { useEffect, useState } from "react";
import { api } from "./api";

type Any = Record<string, unknown>;

function statusBadge(s: string) {
  const cls = s === "resolved" ? "green" : s === "awaiting_approval" || s === "approved" ? "amber" : s === "abstained" ? "red" : "blue";
  return <span className={`badge ${cls}`}>{s.replaceAll("_", " ")}</span>;
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
      <header>
        <h1>🚨 IncidentPilot</h1>
        <span className="muted">Multi-agent incident resolver — LangGraph + Groq</span>
        {health && <span className={`badge ${health.demo_mode ? "amber" : "green"}`}>{health.demo_mode ? "Demo mode — add GROQ_API_KEY for live LLM" : `Live LLM: ${health.model}`}</span>}
        <span style={{ flex: 1 }} />
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
                    <h4>Hypotheses</h4>
                    {(selected.hypotheses as Any[]).map((h) => (
                      <div key={h.id as string} className="evidence"><strong>#{h.rank as number} — {Math.round((h.confidence as number) * 100)}% confidence</strong><br />{h.statement as string}<br /><span className="muted">Evidence: {(h.evidence_ids as string[]).length} linked items · {h.status as string}</span></div>
                    ))}
                    <h4>Evidence</h4>
                    {(selected.evidence as Any[]).slice(0, 6).map((e) => (
                      <div key={e.id as string} className="evidence"><span className="pill">{e.kind as string}</span> <strong>{e.source_ref as string}</strong><div className="mono">{e.content as string}</div></div>
                    ))}
                    {(() => { const plan = selected.plan as Any | null; return plan ? (
                      <>
                        <h4>Remediation plan — {plan.status as string}</h4>
                        {(plan.steps as Any[]).map((s, i) => <div key={i}>• <code>{s.tool as string}</code> — {s.description as string} <span className="muted">(risk: {s.risk as string})</span></div>)}
                        <p className="muted">Rollback: {plan.rollback as string}</p>
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
                    <h4>Agent runs</h4>
                    <table><thead><tr><th>Agent</th><th>Model</th><th>Tokens</th><th>Latency</th></tr></thead><tbody>
                      {(selected.agent_runs as Any[]).map((r, i) => <tr key={i}><td>{r.agent as string}</td><td>{r.model as string}</td><td>{r.tokens as number}</td><td>{r.latency_ms as number} ms</td></tr>)}
                    </tbody></table>
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
  );
}
