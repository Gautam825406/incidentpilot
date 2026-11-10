// API client — base URL comes from VITE_API_URL only, never hardcoded.
// Empty means same-origin (Vite dev proxy forwards /api to the backend).
export const API_URL = (import.meta.env.VITE_API_URL as string) || "";

export type Org = { id: string; name: string; slug: string; plan?: string };
export type AuthUser = { id?: string; name?: string; email: string; role: string; org_id?: string | null };
export type AuthResponse = { token: string; user: AuthUser; organization: Org | null };
export type IntegrationStatusEntry = {
  name: string;
  configured: boolean;
  provider: string;
  masked_url: string | null;
  last_test_ok: boolean | null;
  source: "org" | "global" | "none";
};
export type OrgIntegration = {
  id: string;
  org_id: string;
  name: string;
  provider: string;
  config: Record<string, unknown>;
  secrets_masked: Record<string, string>;
  is_active: boolean;
  last_tested_at: string | null;
  last_test_ok: boolean | null;
};
export type Member = { id: string; name: string; email: string; role: string; org_id?: string | null };
export type OrgMe = {
  organization: Org & { webhook_token_masked?: string | null };
  role: string;
  memberships: { org_id: string; org_name: string | null; org_slug: string | null; role: string }[];
};

const TOKEN_KEY = "ip_token";
const ORG_KEY = "ip_org";
const USER_KEY = "ip_user";

export function token(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

/** The workspace (org) saved at login/signup, or null when not logged in. */
export function getOrg(): Org | null {
  try {
    const raw = localStorage.getItem(ORG_KEY);
    return raw ? (JSON.parse(raw) as Org) : null;
  } catch {
    return null;
  }
}

/** The logged-in user saved at login/signup (email + role only, no secrets). */
export function getStoredUser(): AuthUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as AuthUser) : null;
  } catch {
    return null;
  }
}

/** Persist a login/signup response: JWT + org + user. Call on every auth success. */
export function saveAuth(r: AuthResponse): void {
  localStorage.setItem(TOKEN_KEY, r.token);
  if (r.organization) localStorage.setItem(ORG_KEY, JSON.stringify(r.organization));
  else localStorage.removeItem(ORG_KEY);
  if (r.user) localStorage.setItem(USER_KEY, JSON.stringify(r.user));
}

/** Clear all auth state (sign out). */
export function clearAuth(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(ORG_KEY);
  localStorage.removeItem(USER_KEY);
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
    let msg = text.slice(0, 300);
    try {
      const j = JSON.parse(text) as { detail?: unknown };
      if (typeof j.detail === "string" && j.detail) msg = j.detail;
    } catch {
      /* not JSON — keep the raw text */
    }
    throw new Error(`${r.status}: ${msg}`);
  }
  return r.json();
}
export const api = {
  health: () => req("/api/health"),
  login: (email: string, password: string) =>
    req("/api/auth/login", { method: "POST", headers: headers(), body: JSON.stringify({ email, password }) }),
  signup: (company_name: string, admin_name: string, email: string, password: string) =>
    req("/api/auth/signup", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ company_name, admin_name, email, password }),
    }),
  getOrgMe: () => req("/api/orgs/me", { headers: headers(false) }),
  listMembers: () => req("/api/orgs/members", { headers: headers(false) }),
  addMember: (email: string, role: string) =>
    req("/api/orgs/members", { method: "POST", headers: headers(), body: JSON.stringify({ email, role }) }),
  listOrgIntegrations: () => req("/api/orgs/integrations", { headers: headers(false) }),
  saveIntegration: (name: string, provider: string | undefined, config: Record<string, unknown>, secrets: Record<string, string>) =>
    req(`/api/orgs/integrations/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: headers(),
      body: JSON.stringify({ name, provider, config, secrets }),
    }),
  deleteIntegration: (name: string) =>
    req(`/api/orgs/integrations/${encodeURIComponent(name)}`, { method: "DELETE", headers: headers(false) }),
  testIntegration: (name: string) =>
    req(`/api/integrations/${encodeURIComponent(name)}/test`, { method: "POST", headers: headers() }),
  integrationsStatus: () => req("/api/integrations/status", { headers: headers(false) }),
  rotateWebhookToken: () =>
    req("/api/orgs/webhook-token/rotate", { method: "POST", headers: headers() }),
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
  notify: (id: string, channel: "jira" | "slack") =>
    req(`/api/incidents/${id}/notify`, { method: "POST", headers: headers(), body: JSON.stringify({ channel }) }),
};
