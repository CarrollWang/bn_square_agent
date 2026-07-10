export type SourceType = "binance_square" | "techflow_newsletter";

export interface Account {
  account_key: string;
  name: string;
  enabled: boolean;
  cookie_saved: boolean;
  cookie_length: number;
  cookie_names: string[];
  check_status?: string;
  checked_at?: string;
  check_error?: string;
  proxy_configured: boolean;
  proxy_url_masked: string;
  mcp_url: string;
  mcp_auth_token_configured: boolean;
  created_at: string;
}

export interface AccountDetail {
  account_key: string;
  name: string;
  cookie_saved: boolean;
  proxy_url: string;
  mcp_url: string;
  mcp_auth_token_configured: boolean;
}

export interface CookieImportStartResult {
  ok: boolean;
  session_id: string;
  message: string;
}

export interface CookieImportFinishResult {
  ok: boolean;
  account_key: string;
  cookie_length: number;
  cookie_names: string[];
}

export interface MaterialSource {
  id: number;
  name: string;
  source_type: SourceType;
  url: string;
  enabled: number;
  last_checked_at?: string | null;
  last_error?: string | null;
}

export interface MaterialItem {
  id: number;
  source_name?: string;
  source_type?: SourceType;
  title?: string;
  content: string;
  url?: string;
  author?: string;
  status: string;
  tag_status?: string;
  tag_json?: string;
  created_at: string;
}

export interface PublishHistoryItem {
  material_item_id: number;
  account_key: string;
  account_name: string;
  account_check_status?: string | null;
  status: "published" | "failed" | "skipped";
  generated_id?: number | null;
  attempt_count: number;
  published_at?: string | null;
  last_attempted_at?: string | null;
  last_activity_at?: string | null;
  error?: string | null;
  publish_result?: Record<string, unknown> | string | null;
  material_title?: string | null;
  material_content?: string | null;
  material_url?: string | null;
  source_name?: string | null;
  source_type?: SourceType | null;
  source_created_at?: string | null;
  generated_content?: string | null;
  generated_publish_status?: string | null;
  generated_published_at?: string | null;
}

export interface PublishAccountSummary {
  account_key: string;
  name: string;
  enabled: boolean;
  check_status?: string | null;
  checked_at?: string | null;
  published_count: number;
  failed_count: number;
  skipped_count: number;
  last_published_at?: string | null;
  last_activity_at?: string | null;
}

export interface PerformanceSummary {
  active_accounts: number;
  publishing_accounts: number;
  idle_accounts: number;
  invalid_accounts: number;
  limited_accounts: number;
  total_published: number;
  total_failed: number;
  total_skipped: number;
  success_rate: number;
  avg_attempt_count: number;
}

export interface PerformanceDailyPoint {
  date: string;
  published_count: number;
  failed_count: number;
  skipped_count: number;
  total_count: number;
}

export interface PerformanceAccountMetric {
  account_key: string;
  name: string;
  check_status?: string | null;
  published_count: number;
  failed_count: number;
  skipped_count: number;
  total_runs: number;
  total_attempted: number;
  success_rate: number;
  avg_attempt_count: number;
  active_days: number;
  last_published_at?: string | null;
  last_activity_at?: string | null;
  top_source_name?: string | null;
  top_source_type?: SourceType | null;
  top_source_count: number;
  health_label: string;
  health_tone: "success" | "warning" | "danger" | "info";
  issue_reason?: string | null;
}

export interface PerformanceIssue {
  account_key: string;
  name: string;
  severity: "high" | "medium" | "low";
  severity_label: string;
  reason: string;
}

export interface PerformanceSourceSummary {
  source_name: string;
  source_type?: SourceType | null;
  published_count: number;
  failed_count: number;
  skipped_count: number;
  success_rate: number;
}

export interface AccountPerformanceDashboard {
  period_days: number;
  summary: PerformanceSummary;
  daily: PerformanceDailyPoint[];
  accounts: PerformanceAccountMetric[];
  issues: PerformanceIssue[];
  sources: PerformanceSourceSummary[];
}

export interface MonitorStatus {
  running: boolean;
  auto_monitor_enabled: boolean;
  auto_consume_materials: boolean;
  poll_interval_seconds: number;
  success_interval_seconds: number;
  failure_interval_seconds: number;
  ttl_seconds: number;
  consume_batch_size: number;
  current_stage?: string | null;
  next_run_after_seconds?: number | null;
  next_run_reason?: string | null;
  last_started_at?: string | null;
  last_finished_at?: string | null;
  expired_count: number;
  last_error?: string | null;
  consecutive_publish_failures: number;
  publish_failure_alert_threshold: number;
  last_alert_at?: string | null;
  last_alert_sent: boolean;
  last_alert_error?: string | null;
  last_results: any[];
  last_tag_results: any[];
  last_consume_results: any[];
}

export interface Settings {
  llm_api_key_configured: boolean;
  llm_api_key_masked: string;
  llm_base_url: string;
  llm_model: string;
  llm_model_options: string[];
  embedding_provider: "openai" | "dashscope";
  embedding_api_key_configured: boolean;
  embedding_api_key_masked: string;
  embedding_uses_llm_credentials: boolean;
  embedding_base_url: string;
  embedding_model: string;
  mcp_url: string;
  mcp_publish_tool: string;
  mcp_auth_token_configured: boolean;
  mcp_auth_token_masked: string;
  auto_monitor_enabled: boolean;
  auto_publish: boolean;
  auto_consume_materials: boolean;
  material_poll_interval_seconds: number;
  material_success_interval_seconds: number;
  material_failure_interval_seconds: number;
  material_ttl_seconds: number;
  material_consume_batch_size: number;
  publish_failure_alert_threshold: number;
  alert_email_enabled: boolean;
  alert_email_to: string;
  smtp_host: string;
  smtp_port: number;
  smtp_username: string;
  smtp_password_configured: boolean;
  smtp_password_masked: string;
  smtp_from: string;
  smtp_use_tls: boolean;
}
