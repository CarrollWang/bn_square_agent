<template>
  <div class="history-page">
    <div class="toolbar">
      <div class="toolbar-title">
        <strong>发文历史</strong>
        <span>看清每个账号发了什么、哪里失败、最近有没有持续产出</span>
      </div>
      <el-space wrap>
        <el-select v-model="filters.account_key" placeholder="全部账号" clearable style="width: 180px">
          <el-option v-for="item in summaries" :key="item.account_key" :label="item.name" :value="item.account_key" />
        </el-select>
        <el-select v-model="filters.status" placeholder="全部状态" clearable style="width: 160px">
          <el-option label="已发布" value="published" />
          <el-option label="失败" value="failed" />
          <el-option label="跳过" value="skipped" />
        </el-select>
        <el-button plain @click="loadHistory">筛选</el-button>
        <el-button plain @click="resetFilters">重置</el-button>
        <el-button type="primary" plain :loading="loading" @click="refreshAll">刷新</el-button>
      </el-space>
    </div>

    <div class="metric-grid">
      <div class="metric-card">
        <strong>{{ summaries.length }}</strong>
        <span>活跃账号</span>
      </div>
      <div class="metric-card">
        <strong>{{ totalPublished }}</strong>
        <span>累计成功</span>
      </div>
      <div class="metric-card">
        <strong>{{ totalFailed }}</strong>
        <span>累计失败</span>
      </div>
      <div class="metric-card">
        <strong>{{ totalSkipped }}</strong>
        <span>累计跳过</span>
      </div>
    </div>

    <el-card class="page-card" shadow="never">
      <template #header>
        <div class="toolbar">
          <div class="toolbar-title">
            <strong>账号汇总</strong>
            <span>优先看哪些账号真的在稳定出稿</span>
          </div>
        </div>
      </template>
      <el-table :data="summaries" border stripe>
        <el-table-column label="账号" min-width="180">
          <template #default="{ row }">
            <strong>{{ row.name }}</strong>
            <div class="muted">key: {{ row.account_key }}</div>
          </template>
        </el-table-column>
        <el-table-column label="检测" width="130">
          <template #default="{ row }">
            <el-tag :type="checkStatusType(row.check_status)" effect="plain">
              {{ row.check_status || "unchecked" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="published_count" label="成功" width="100" />
        <el-table-column prop="failed_count" label="失败" width="100" />
        <el-table-column prop="skipped_count" label="跳过" width="100" />
        <el-table-column label="最近成功" width="180">
          <template #default="{ row }">{{ formatTime(row.last_published_at) }}</template>
        </el-table-column>
        <el-table-column label="最近活动" width="180">
          <template #default="{ row }">{{ formatTime(row.last_activity_at) }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card class="page-card" shadow="never">
      <template #header>
        <div class="toolbar">
          <div class="toolbar-title">
            <strong>明细记录</strong>
            <span>最新 {{ history.length }} 条发文结果</span>
          </div>
        </div>
      </template>
      <el-table :data="history" border stripe>
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ formatTime(row.last_activity_at) }}</template>
        </el-table-column>
        <el-table-column label="账号" width="160">
          <template #default="{ row }">
            <strong>{{ row.account_name }}</strong>
            <div class="muted">{{ row.account_key }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="publishStatusType(row.status)" effect="plain">
              {{ publishStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="素材" min-width="320">
          <template #default="{ row }">
            <div class="cell-title">{{ row.material_title || `material#${row.material_item_id}` }}</div>
            <div class="muted">{{ row.source_name || sourceTypeLabel(row.source_type || undefined) }}</div>
            <div class="material-snippet">{{ shortText(row.material_content || "", 120) }}</div>
            <el-link v-if="row.material_url" :href="row.material_url" target="_blank" type="primary">原文链接</el-link>
          </template>
        </el-table-column>
        <el-table-column label="终稿 / 结果" min-width="340">
          <template #default="{ row }">
            <div class="cell-title">{{ shortText(row.generated_content || "未生成终稿", 140) }}</div>
            <div class="muted">终稿#{{ row.generated_id || "-" }}，尝试 {{ row.attempt_count }} 次</div>
            <div class="result-text">{{ rowResultText(row) }}</div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ElMessage } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { api } from "@/api";
import type { PublishAccountSummary, PublishHistoryItem } from "@/types";
import { formatTime, publishErrorText, shortText, sourceTypeLabel } from "@/utils";

const loading = ref(false);
const summaries = ref<PublishAccountSummary[]>([]);
const history = ref<PublishHistoryItem[]>([]);
const filters = reactive({
  account_key: "",
  status: "",
});

const totalPublished = computed(() =>
  summaries.value.reduce((sum, item) => sum + item.published_count, 0),
);
const totalFailed = computed(() =>
  summaries.value.reduce((sum, item) => sum + item.failed_count, 0),
);
const totalSkipped = computed(() =>
  summaries.value.reduce((sum, item) => sum + item.skipped_count, 0),
);

function publishStatusLabel(status: string) {
  return (
    {
      published: "已发布",
      failed: "失败",
      skipped: "跳过",
    }[status] || status || "-"
  );
}

function publishStatusType(status: string) {
  return (
    {
      published: "success",
      failed: "danger",
      skipped: "warning",
    }[status] || "info"
  );
}

function checkStatusType(status?: string | null) {
  return (
    {
      valid: "success",
      invalid: "danger",
      unchecked: "info",
    }[status || ""] || "warning"
  );
}

function rowResultText(row: PublishHistoryItem) {
  if (row.status === "published") {
    return "发布成功";
  }
  if (row.status === "skipped") {
    return row.error || "账号当前被跳过";
  }
  if (row.error) {
    return row.error;
  }
  if (row.publish_result && typeof row.publish_result === "object") {
    const text = publishErrorText(row.publish_result);
    if (text) return text;
  }
  if (typeof row.publish_result === "string") {
    return row.publish_result;
  }
  return "发布失败";
}

async function loadSummaries() {
  summaries.value = await api.publishAccountSummaries();
}

async function loadHistory() {
  history.value = await api.publishHistory({
    limit: 120,
    account_key: filters.account_key || undefined,
    status: filters.status || undefined,
  });
}

async function refreshAll() {
  loading.value = true;
  try {
    await Promise.all([loadSummaries(), loadHistory()]);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "加载失败");
  } finally {
    loading.value = false;
  }
}

async function resetFilters() {
  filters.account_key = "";
  filters.status = "";
  await loadHistory();
}

onMounted(refreshAll);
</script>

<style scoped>
.history-page {
  display: grid;
  gap: 16px;
}

.cell-title {
  color: #0f172a;
  font-weight: 600;
}

.material-snippet,
.result-text {
  margin-top: 6px;
  color: #475569;
  line-height: 1.5;
}
</style>
