type Any = Record<string, unknown>;

export default function Landing({ health, onEnter }: { health: Any | null; onEnter: () => void }) {
  const demo = health ? Boolean(health.demo_mode) : true;
  return (
    <div className="landing">
      <header className="landing-nav">
        <div className="brand"><span className="brand-mark">🚨</span><span>IncidentPilot</span></div>
        <nav>
          <a href="#how">How it works</a>
          <a href="#features">Features</a>
          <a href="#results">Results</a>
        </nav>
        <button className="primary" onClick={onEnter}>Open console →</button>
      </header>

      <section className="hero">
        <div className="hero-copy">
          <span className="badge blue">LangGraph · Groq · FastAPI · React</span>
          <h1>
            Incidents, <span className="grad">investigated</span><br />
            before they wake you up.
          </h1>
          <p className="lede">
            IncidentPilot is a multi-agent production incident resolver. A LangGraph
            supervisor sends Triage, Logs, Metrics and Knowledge agents in parallel,
            ranks evidence-linked root causes, and proposes a fix — nothing executes
            without your approval.
          </p>
          <div className="row">
            <button className="primary big" onClick={onEnter}>🔥 Try the live demo</button>
            <a className="ghost-btn" href="https://github.com/Gautam825406/incidentpilot" target="_blank" rel="noreferrer">View on GitHub</a>
          </div>
          <p className="muted">
            {demo
              ? "Running in demo mode — add a GROQ_API_KEY for live LLM. Simulated services only, no real production action is ever taken."
              : `Live LLM: ${String(health?.model ?? "")} — simulated services only.`}
          </p>
          <div className="hero-stats">
            <div><strong>6/6</strong><span>Top-1 root cause<br />(demo eval, n=6)</span></div>
            <div><strong>0</strong><span>Unsafe actions<br />(demo eval)</span></div>
            <div><strong>1.0</strong><span>Evidence coverage<br />(demo eval)</span></div>
            <div><strong>1,837 ms</strong><span>Median time to<br />hypothesis (demo)</span></div>
          </div>
        </div>
        <div className="hero-panel card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <strong>Live incident flow</strong>
            <span className="badge amber">awaiting approval</span>
          </div>
          <div className="flow">
            <div><span className="pill">1 · Alert</span><strong>checkout-oom</strong><span className="muted">OutOfMemoryError in checkout-api</span></div>
            <div><span className="pill">2 · Investigate</span><strong>91% confidence</strong><span className="muted">Memory leak in deploy v2.15.0 · 7 evidence items</span></div>
            <div><span className="pill">3 · Approve</span><strong>Human in the loop</strong><span className="muted">You approve the plan — nothing runs without you</span></div>
            <div><span className="pill">4 · Execute &amp; verify</span><strong>Rollback + health check</strong><span className="muted">Error rate 18.7% → 0.8% · verification passed</span></div>
          </div>
          <button className="primary" style={{ width: "100%" }} onClick={onEnter}>Open the console and run it yourself</button>
          <p className="muted">Demo login: admin@demo.local / admin123</p>
        </div>
      </section>

      <section id="how" className="section">
        <h2>How it works</h2>
        <p className="muted">Alert → investigation → plan → approval → guarded execution → verification → postmortem.</p>
        <div className="feature-grid">
          <div className="card"><h3>🔍 Investigate</h3><p>Four specialist agents read logs, metrics and past runbooks in parallel, then agree on ranked, evidence-linked hypotheses.</p></div>
          <div className="card"><h3>🧑‍⚖️ Approve</h3><p>Every plan waits for a human. Approve, reject, or edit it — execution is allow-list only, with dry-run first.</p></div>
          <div className="card"><h3>🛡️ Execute safely</h3><p>Only restart, rollback, scale and health-check are allowed. Approval tokens expire in 30 minutes, and every step is idempotent.</p></div>
          <div className="card"><h3>📋 Learn</h3><p>Verification checks the error rate, then a postmortem draft feeds back into the knowledge base for the next incident.</p></div>
        </div>
      </section>

      <section id="features" className="section">
        <h2>Built for real production thinking</h2>
        <div className="feature-grid">
          <div className="card"><h3>Multi-agent, not one prompt</h3><p>LangGraph supervisor + Triage, Logs, Metrics and Knowledge agents, with MCP-style typed tools.</p></div>
          <div className="card"><h3>Evidence, not vibes</h3><p>Every hypothesis links its evidence. If the signal is unknown, the system abstains instead of guessing.</p></div>
          <div className="card"><h3>Measured, not claimed</h3><p>A golden-set eval harness stores real run numbers — accuracy, evidence coverage, unsafe actions, latency.</p></div>
          <div className="card"><h3>Safe by design</h3><p>No unapproved execution, no deletions, no production DB writes. Simulated services only in v1.</p></div>
        </div>
      </section>

      <section id="results" className="section">
        <h2>Measured results — demo mode</h2>
        <p className="muted">Golden set, 6 cases, demo model. These are demo-mode numbers, not live-LLM results — re-run the eval with a working Groq key for live numbers.</p>
        <div className="hero-stats">
          <div><strong>6/6</strong><span>Top-1 accuracy</span></div>
          <div><strong>1/1</strong><span>Abstention correct</span></div>
          <div><strong>0</strong><span>Unsafe actions</span></div>
          <div><strong>11.44 s</strong><span>Total eval time</span></div>
        </div>
        <div className="row" style={{ marginTop: 18 }}>
          <button className="primary big" onClick={onEnter}>Run it in the console →</button>
        </div>
      </section>

      <footer className="landing-footer">
        <span>IncidentPilot v1 · Simulated services only · Jira &amp; Slack optional via backend env</span>
        <span>Built by Gautam Keshri · <a href="https://github.com/Gautam825406/incidentpilot" target="_blank" rel="noreferrer">GitHub</a></span>
      </footer>
    </div>
  );
}
