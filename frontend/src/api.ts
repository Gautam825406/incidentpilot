// API client — base URL comes from VITE_API_URL only, never hardcoded.
// Empty means same-origin (Vite dev proxy forwards /api to the backend).
export const API_URL = (import.meta.env.VITE_API_URL as string) || "";

export function token(): string | null {
  return localStorage.getItem("ip_token");
}
function headers(json = true): Record<string, string> {
  const h: Record<string, string> = {};
  if (json) h["Content-Type"] = "application/json";
  const t = token();
  if (t) h["Authorization"] = `Bearer ${t}`;
  return h;
}
async function req(path: string, init?: RequestInit) {
  const r = await fetch(`${API_URL}${path}`, init);
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status}: ${text.slice(0, 300)}`);
  }
  return r.json();
}
export const api = {
  health: () => req("/api/health"),
  login: (email: string, password: string) =>
    req("/api/auth/login", { method: "POST", headers: headers(), body: JSON.stringify({ email, password }) }),
  scenarios: () => req("/api/scenarios"),
  incidents: () => req("/api/incidents"),
  incident: (id: string) => req(`/api/incidents/${id}`),
  simulate: (scenario: string) =>
    req("/api/simulate", { method: "POST", headers: headers(), body: JSON.stringify({ scenario }) }),
  investigate: (id: string) => req(`/api/incidents/${id}/investigate`, { method: "POST", headers: headers() }),
  approve: (planId: string) => req(`/api/plans/${planId}/approve`, { method: "POST", headers: headers() }),
  reject: (planId: string, reason: string) =>
    req(`/api/plans/${planId}/reject`, { method: "POST", headers: headers(), body: JSON.stringify({ reason }) }),
  execute: (planId: string, dryRun: boolean) =>
    req(`/api/plans/${planId}/execute`, { method: "POST", headers: headers(), body: JSON.stringify({ dry_run: dryRun }) }),
  verify: (id: string) => req(`/api/incidents/${id}/verify`, { method: "POST", headers: headers() }),
  evalResults: () => req("/api/evals/results"),
  runEval: () => req("/api/evals/run", { method: "POST", headers: headers() }),
  documents: () => req("/api/documents"),
  integrationsStatus: () => req("/api/integrations/status"),
  notify: (id: string, channel: "jira" | "slack") =>
    req(`/api/incidents/${id}/notify`, { method: "POST", headers: headers(), body: JSON.stringify({ channel }) }),
};
