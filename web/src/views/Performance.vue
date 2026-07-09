<template>
  <div class="performance-page">
    <div class="toolbar">
      <div class="toolbar-title">
        <strong>账号表现看板</strong>
        <span>按时间窗口看账号产出、成功率、限制情况和需要优先处理的问题</span>
      </div>
      <el-space wrap>
        <el-select v-model="selectedDays" style="width: 150px" @change="loadDashboard">
          <el-option label="近 7 天" :value="7" />
          <el-option label="近 30 天" :value="30" />
          <el-option label="近 90 天" :value="90" />
        </el-select>
        <el-button type="primary" plain :loading="loading" @click="loadDashboard">刷新</el-button>
      </el-space>
    </div>

    <div class="metric-grid">
      <div class="metric-card">
        <strong>{{ dashboard?.summary.active_accounts || 0 }}</strong>
        <span>启用账号</span>
      </div>
      <div class="metric-card">
        <strong>{{ dashboard?.summary.publishing_accounts || 0 }}</strong>
        <span>{{ selectedDays }} 天内有成功账号</span>
      </div>
      <div class="metric-card">
        <strong>{{ dashboard?.summary.total_published || 0 }}</strong>
        <span>{{ selectedDays }} 天累计成功</span>
      </div>
      <div class="metric-card">
        <strong>{{ dashboard ? `${dashboard.summary.success_rate}%` : "0%" }}</strong>
        <span>整体成功率</span>
      </div>
      <div class="metric-card">
        <strong>{{ dashboard?.summary.idle_accounts || 0 }}</strong>
        <span>空闲账号</span>
      </div>
      <div class="metric-card">
        <strong>{{ dashboard?.summary.invalid_accounts || 0 }}</strong>
        <span>失效账号</span>
      </div>
    </div>

    <div class="board-grid">
      <el-card class="page-card" shadow="never">
        <template #header>
          <div class="toolbar">
            <div class="toolbar-title">
              <strong>日趋势</strong>
              <span>绿色是成功，红色是失败，黄色是跳过</span>
            </div>
          </div>
        </template>
        <div class="trend-panel">
          <div class="trend-legend">
            <span><i class="dot published"></i>成功</span>
            <span><i class="dot failed"></i>失败</span>
            <span><i class="dot skipped"></i>跳过</span>
          </div>
          <div class="trend-chart">
            <div v-for="point in dashboard?.daily || []" :key="point.date" class="trend-column">
              <div class="trend-canvas">
                <div class="trend-stack" :style="{ height: `${barHeight(point.total_count)}%` }">
                  <div v-if="point.published_count" class="trend-segment published" :style="{ flex: point.published_count }"></div>
                  <div v-if="point.failed_count" class="trend-segment failed" :style="{ flex: point.failed_count }"></div>
                  <div v-if="point.skipped_count" class="trend-segment skipped" :style="{ flex: point.skipped_count }"></div>
                </div>
              </div>
              <div class="trend-count">{{ point.total_count || "-" }}</div>
              <div class="trend-label">{{ shortDay(point.date) }}</div>
            </div>
          </div>
        </div>
      </el-card>

      <el-card class="page-card" shadow="never">
        <template #header>
          <div class="toolbar">
            <div class="toolbar-title">
              <strong>优先处理</strong>
              <span>先把这些账号处理掉，整体稳定性会提升更快</span>
            </div>
          </div>
        </template>
        <div v-if="dashboard?.issues?.length" class="issues-list">
          <div v-for="issue in dashboard.issues" :key="`${issue.account_key}-${issue.severity}`" class="issue-item">
            <div class="issue-head">
              <strong>{{ issue.name }}</strong>
              <el-tag :type="severityTagType(issue.severity)" effect="plain">{{ issue.severity_label }}</el-tag>
            </div>
            <div class="muted">key: {{ issue.account_key }}</div>
            <div class="issue-reason">{{ issue.reason }}</div>
          </div>
        </div>
        <el-empty v-else description="当前没有明显异常账号" :image-size="84" />
      </el-card>
    </div>

    <div class="leader-grid">
      <el-card class="page-card" shadow="never">
        <template #header>
          <div class="toolbar">
            <div class="toolbar-title">
              <strong>账号排名</strong>
              <span>先看谁真的在稳定出量，再看谁需要修</span>
            </div>
          </div>
        </template>
        <el-table :data="dashboard?.accounts || []" border stripe>
          <el-table-column label="账号" min-width="180">
            <template #default="{ row }">
              <strong>{{ row.name }}</strong>
              <div class="muted">key: {{ row.account_key }}</div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag :type="row.health_tone" effect="plain">{{ row.health_label }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="成功率" width="110">
            <template #default="{ row }">{{ row.total_attempted ? `${row.success_rate}%` : "-" }}</template>
          </el-table-column>
          <el-table-column prop="published_count" label="成功" width="90" />
          <el-table-column prop="failed_count" label="失败" width="90" />
          <el-table-column prop="skipped_count" label="跳过" width="90" />
          <el-table-column label="活跃天数" width="100">
            <template #default="{ row }">{{ row.active_days }}</template>
          </el-table-column>
          <el-table-column label="主力来源" min-width="180">
            <template #default="{ row }">
              <div>{{ row.top_source_name || "-" }}</div>
              <div class="muted">
                {{ row.top_source_name ? `${sourceTypeLabel(row.top_source_type || undefined)} · ${row.top_source_count} 次` : "暂无成功来源" }}
              </div>
            </template>
          </el-table-column>
          <el-table-column label="最近成功" width="180">
            <template #default="{ row }">{{ formatTime(row.last_published_at) }}</template>
          </el-table-column>
          <el-table-column label="关注点" min-width="220">
            <template #default="{ row }">
              <span class="issue-inline">{{ row.issue_reason || "当前表现正常" }}</span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card class="page-card" shadow="never">
        <template #header>
          <div class="toolbar">
            <div class="toolbar-title">
              <strong>来源效果</strong>
              <span>看哪些来源更容易带来稳定成功</span>
            </div>
          </div>
        </template>
        <el-table :data="dashboard?.sources || []" border stripe>
          <el-table-column label="来源" min-width="200">
            <template #default="{ row }">
              <strong>{{ row.source_name }}</strong>
              <div class="muted">{{ sourceTypeLabel(row.source_type || undefined) }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="published_count" label="成功" width="90" />
          <el-table-column prop="failed_count" label="失败" width="90" />
          <el-table-column prop="skipped_count" label="跳过" width="90" />
          <el-table-column label="成功率" width="110">
            <template #default="{ row }">{{ row.success_rate }}%</template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ElMessage } from "element-plus";
import { computed, onMounted, ref } from "vue";

import { api } from "@/api";
import type { AccountPerformanceDashboard } from "@/types";
import { formatTime, sourceTypeLabel } from "@/utils";

const loading = ref(false);
const selectedDays = ref(7);
const dashboard = ref<AccountPerformanceDashboard | null>(null);

const maxDailyTotal = computed(() =>
  Math.max(1, ...(dashboard.value?.daily || []).map((item) => item.total_count || 0)),
);

function barHeight(totalCount: number) {
  if (!totalCount) return 8;
  return Math.max(8, Math.round((totalCount / maxDailyTotal.value) * 100));
}

function shortDay(value: string) {
  return value.slice(5);
}

function severityTagType(severity: string) {
  return (
    {
      high: "danger",
      medium: "warning",
      low: "info",
    }[severity] || "info"
  );
}

async function loadDashboard() {
  loading.value = true;
  try {
    dashboard.value = await api.accountPerformance(selectedDays.value);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "加载失败");
  } finally {
    loading.value = false;
  }
}

onMounted(loadDashboard);
</script>

<style scoped>
.performance-page {
  display: grid;
  gap: 16px;
}

.board-grid,
.leader-grid {
  display: grid;
  gap: 16px;
}

.board-grid {
  grid-template-columns: minmax(0, 1.8fr) minmax(300px, 1fr);
}

.trend-panel {
  display: grid;
  gap: 14px;
}

.trend-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  color: #475569;
  font-size: 12px;
}

.dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  margin-right: 6px;
  border-radius: 999px;
}

.published {
  background: #0f9f6e;
}

.failed {
  background: #dc2626;
}

.skipped {
  background: #d97706;
}

.trend-chart {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(28px, 1fr));
  gap: 8px;
  align-items: end;
}

.trend-column {
  display: grid;
  gap: 6px;
  justify-items: center;
}

.trend-canvas {
  display: flex;
  align-items: end;
  width: 100%;
  height: 160px;
  padding: 0 2px;
  border-radius: 10px;
  background:
    linear-gradient(to top, rgba(226, 232, 240, 0.65), rgba(255, 255, 255, 0.2)),
    linear-gradient(to right, rgba(15, 23, 42, 0.02), rgba(15, 23, 42, 0));
}

.trend-stack {
  display: flex;
  flex-direction: column-reverse;
  width: 100%;
  min-height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(226, 232, 240, 0.85);
}

.trend-segment {
  width: 100%;
}

.trend-count {
  color: #0f172a;
  font-size: 12px;
  font-weight: 600;
}

.trend-label {
  color: #64748b;
  font-size: 11px;
}

.issues-list {
  display: grid;
  gap: 12px;
}

.issue-item {
  padding: 12px;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  background: linear-gradient(180deg, #ffffff, #f8fafc);
}

.issue-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.issue-head strong {
  color: #0f172a;
}

.issue-reason,
.issue-inline {
  margin-top: 6px;
  color: #475569;
  line-height: 1.5;
}

@media (max-width: 1080px) {
  .board-grid {
    grid-template-columns: 1fr;
  }
}
</style>
