import type { MonitorStatus } from "./types";

export function formatTime(value?: string | null) {
  if (!value) return "-";
  return new Date(value).toLocaleString();
}

export function shortText(value?: string | null, maxLength = 22) {
  const text = value || "-";
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}...` : text;
}

export function sourceTypeLabel(sourceType?: string) {
  return (
    {
      binance_square: "BN 广场作者",
      techflow_newsletter: "TechFlow 深潮快讯",
    }[sourceType || ""] || sourceType || "-"
  );
}

export function nextRunLabel(status?: MonitorStatus | null) {
  if (!status) return "-";
  if (!status.next_run_after_seconds) return status.running ? "本轮运行中" : "-";
  const reasonMap: Record<string, string> = {
    poll: "采集轮询",
    published: "成功节奏",
    publish_failed: "失败重试",
    collect_failed: "采集重试",
    locked: "等待其他实例",
    paused: "已暂停",
    paused_after_failures: "连续失败暂停",
    error: "异常重试",
  };
  return `${status.next_run_after_seconds}s ${reasonMap[status.next_run_reason || ""] || ""}`.trim();
}

export function publishErrorText(result: any) {
  if (!result) return "";
  const structured = result.structuredContent;
  if (structured?.error) return structured.error;
  if (result.error) return result.error;
  for (const item of result.content || []) {
    if (!item?.text) continue;
    try {
      const payload = JSON.parse(item.text);
      if (payload.error) return payload.error;
    } catch {
      // Plain text tool output.
    }
  }
  return "";
}

export function formatMonitorLogs(status?: MonitorStatus | null) {
  if (!status) return "监控状态加载中...";
  const lines: string[] = [];
  lines.push(`[${new Date().toLocaleString()}] 自动运行状态：${status.running ? "运行中" : "等待下一轮"}`);
  lines.push(`采集间隔：${status.poll_interval_seconds}s；成功间隔：${status.success_interval_seconds}s；失败重试：${status.failure_interval_seconds}s`);
  lines.push(`素材有效期：${status.ttl_seconds}s；每轮消费：${status.consume_batch_size}`);
  lines.push(`自动消费：${status.auto_consume_materials ? "开启" : "关闭"}`);
  if (status.current_stage) lines.push(`当前阶段：${status.current_stage}`);
  lines.push(`下一轮：${nextRunLabel(status)}`);
  lines.push(`连续发文失效：${status.consecutive_publish_failures || 0}/${status.publish_failure_alert_threshold || 5}`);
  if (status.last_alert_at) {
    lines.push(
      `邮件提醒：${status.last_alert_sent ? "已发送" : "未发送"} ${formatTime(status.last_alert_at)}${
        status.last_alert_error ? `，原因：${status.last_alert_error}` : ""
      }`,
    );
  }
  lines.push(`上次开始：${formatTime(status.last_started_at)}`);
  lines.push(`上次结束：${formatTime(status.last_finished_at)}`);
  lines.push(`过期清理：${status.expired_count || 0} 条`);
  if (status.last_error) lines.push(`错误：${status.last_error}`);

  lines.push("");
  lines.push("采集日志：");
  if ((status.last_results || []).length) {
    for (const item of status.last_results) {
      const sourceLabel = item.source_id === "all" ? "全部源" : `source#${item.source_id}`;
      const errorText = item.error ? `，错误：${item.error}` : "";
      lines.push(`- ${sourceLabel}: 找到 ${item.found ?? 0} 条，新增 ${item.inserted ?? 0} 条${errorText}`);
    }
  } else {
    lines.push("- 暂无采集记录");
  }

  lines.push("");
  lines.push("打标日志：");
  if ((status.last_tag_results || []).length) {
    const tagResults = status.last_tag_results || [];
    const acceptedCount = tagResults.filter((item: any) => item.tag_status === "accepted").length;
    const rejectedCount = tagResults.filter((item: any) => item.tag_status === "rejected").length;
    const failedCount = tagResults.filter((item: any) => item.tag_status === "failed").length;
    lines.push(
      `- 本轮处理 ${tagResults.length} 条：通过 ${acceptedCount}，拒绝 ${rejectedCount}，失败 ${failedCount}`,
    );
    for (const item of status.last_tag_results.slice(0, 12)) {
      const tag = item.tag || {};
      const symbol = tag.symbol || tag.token || "-";
      const title = shortText(item.title, 42);
      if (item.tag_status === "failed") {
        lines.push(`- material#${item.material_item_id}: 失败；原因=${item.error || "unknown"}；标题=${title}`);
        continue;
      }
      const reasons = (tag.reasons || []).join(", ") || "-";
      lines.push(
        `- material#${item.material_item_id}: ${item.tag_status === "accepted" ? "通过" : "拒绝"}；` +
          `原因=${reasons}；币种=${symbol}；方向=${tag.direction || "unknown"}；标题=${title}`,
      );
    }
  } else {
    const insertedCount = (status.last_results || []).reduce(
      (total: number, item: any) => total + Number(item.inserted || 0),
      0,
    );
    lines.push("- 本轮处理 0 条：通过 0，拒绝 0，失败 0");
    lines.push(
      insertedCount > 0
        ? `- 新采集 ${insertedCount} 条，但当前无待打标素材`
        : "- 本轮未新增素材，待打标队列为空",
    );
  }

  lines.push("");
  lines.push("消费/发布日志：");
  if ((status.last_consume_results || []).length) {
    for (const item of status.last_consume_results) {
      lines.push(`- material#${item.material_item_id}: ${item.title || "-"}`);
      for (const run of item.runs || []) {
        let result = "";
        if (run.status === "skipped") {
          result = `跳过：${run.skipped_reason || "账号当前不可用"}`;
        } else if (run.status === "already_published") {
          result = `终稿#${run.approved_generated_id || "-"}，此前已发布`;
        } else if (run.error) {
          result = `失败：${run.error}`;
        } else if (run.publish_success) {
          result = `终稿#${run.approved_generated_id || "-"}，发布：成功`;
        } else {
          result = `终稿#${run.approved_generated_id || "-"}，发布：失败 ${publishErrorText(run.publish_result)}`;
        }
        lines.push(`  · 账号 ${run.account_key}: ${result}`);
      }
    }
  } else {
    lines.push("- 暂无消费记录");
  }
  return lines.join("\n");
}