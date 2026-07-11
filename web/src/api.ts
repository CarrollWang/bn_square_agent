import type {
  Account,
  AccountPerformanceDashboard,
  AccountDetail,
  CookieImportFinishResult,
  CookieImportStartResult,
  MaterialItem,
  MaterialSource,
  MonitorStatus,
  PublishAccountSummary,
  PublishHistoryItem,
  Settings,
} from "./types";

async function requestJson<T>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || data.error || response.statusText);
  }
  return data as T;
}

export const api = {
  accounts: () => requestJson<Account[]>("/api/accounts"),
  account: (accountKey: string) =>
    requestJson<AccountDetail>(`/api/accounts/${encodeURIComponent(accountKey)}`),
  saveAccount: (payload: {
    account_key: string;
    name?: string;
    cookie?: string | null;
    proxy_url?: string;
    mcp_url?: string;
    mcp_auth_token?: string | null;
  }) =>
    requestJson<{ ok: boolean }>("/api/accounts", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteAccount: (accountKey: string) =>
    requestJson<{ ok: boolean }>(`/api/accounts/${encodeURIComponent(accountKey)}`, {
      method: "DELETE",
    }),
  checkAccount: (accountKey: string) =>
    requestJson<any>(`/api/accounts/${encodeURIComponent(accountKey)}/check`, {
      method: "POST",
    }),
  startCookieImport: (payload: {
    account_key: string;
    name?: string;
    login_url?: string;
    proxy_url?: string;
  }) =>
    requestJson<CookieImportStartResult>("/api/accounts/import-cookie/start", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  finishCookieImport: (session_id: string) =>
    requestJson<CookieImportFinishResult>("/api/accounts/import-cookie/finish", {
      method: "POST",
      body: JSON.stringify({ session_id }),
    }),
  cancelCookieImport: (session_id: string) =>
    requestJson<{ ok: boolean }>("/api/accounts/import-cookie/cancel", {
      method: "POST",
      body: JSON.stringify({ session_id }),
    }),
  settings: () => requestJson<Settings>("/api/settings"),
  saveSettings: (payload: Record<string, unknown>) =>
    requestJson<{ ok: boolean; saved: string[] }>("/api/settings", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  testLlm: () => requestJson<{ ok: boolean; message: string }>("/api/settings/test-llm", { method: "POST" }),
  testEmbedding: () =>
    requestJson<{ ok: boolean; message: string }>("/api/settings/test-embedding", { method: "POST" }),
  models: () => requestJson<{ ok: boolean; models: string[]; message?: string }>("/api/settings/models", { method: "POST" }),
  mcpTools: () => requestJson<any>("/api/mcp/tools"),
  materialSources: () => requestJson<MaterialSource[]>("/api/material-sources"),
  saveMaterialSource: (payload: Record<string, unknown>) =>
    requestJson<{ ok: boolean; source_id: number }>("/api/material-sources", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteMaterialSource: (sourceId: number) =>
    requestJson<{ ok: boolean }>(`/api/material-sources/${sourceId}`, { method: "DELETE" }),
  checkMaterialSources: () =>
    requestJson<any>("/api/material-sources/check", { method: "POST" }),
  checkMaterialSource: (sourceId: number) =>
    requestJson<any>(`/api/material-sources/${sourceId}/check`, { method: "POST" }),
  materialItems: (limit = 80) =>
    requestJson<MaterialItem[]>(`/api/material-items?status=new&limit=${limit}`),
  publishHistory: (params: { limit?: number; account_key?: string; status?: string } = {}) => {
    const search = new URLSearchParams();
    if (params.limit) search.set("limit", String(params.limit));
    if (params.account_key) search.set("account_key", params.account_key);
    if (params.status) search.set("status", params.status);
    const query = search.toString();
    return requestJson<PublishHistoryItem[]>(`/api/history/publishes${query ? `?${query}` : ""}`);
  },
  publishAccountSummaries: () =>
    requestJson<PublishAccountSummary[]>("/api/history/accounts"),
  accountPerformance: (days = 7) =>
    requestJson<AccountPerformanceDashboard>(`/api/performance/accounts?days=${days}`),
  monitor: () => requestJson<MonitorStatus>("/api/material-monitor"),
  setMonitorEnabled: (enabled: boolean) =>
    requestJson<{ ok: boolean; enabled: boolean }>("/api/material-monitor/enabled", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  runMaterialMonitor: () =>
    requestJson<any>("/api/material-sources/check", { method: "POST" }),
  runMaterialItem: (material_item_id: number) =>
    requestJson<any>("/api/material-items/run", {
      method: "POST",
      body: JSON.stringify({ material_item_id, auto_publish: true }),
    }),
};
