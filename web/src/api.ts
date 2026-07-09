import type {
  Account,
  AccountDetail,
  MaterialItem,
  MaterialSource,
  MonitorStatus,
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
