<template>
  <div class="queue-page">
    <div class="toolbar">
      <div class="toolbar-title">
        <strong>待发布</strong>
        <span>已批准稿件按账号间隔与每日配额依次发布</span>
      </div>
      <el-space wrap>
        <el-select v-model="accountKey" clearable placeholder="全部账号" style="width: 180px" @change="loadQueue">
          <el-option v-for="item in accounts" :key="item.account_key" :label="item.name" :value="item.account_key" />
        </el-select>
        <el-button plain :loading="loading" @click="loadQueue">刷新</el-button>
      </el-space>
    </div>

    <el-card class="page-card" shadow="never">
      <el-table :data="items" border stripe>
        <el-table-column label="账号" width="160">
          <template #default="{ row }"><strong>{{ accountName(row.account_key) }}</strong></template>
        </el-table-column>
        <el-table-column label="素材" min-width="220">
          <template #default="{ row }">
            <div class="cell-title">{{ row.material_title || `material#${row.material_item_id || '-'}` }}</div>
            <div class="muted">{{ row.source_name || "手动输入" }}</div>
          </template>
        </el-table-column>
        <el-table-column label="稿件" min-width="360">
          <template #default="{ row }">{{ shortText(row.content, 180) }}</template>
        </el-table-column>
        <el-table-column label="计划时间" width="190">
          <template #default="{ row }">{{ formatTime(row.scheduled_at) }}</template>
        </el-table-column>
        <el-table-column label="今日配额" width="190">
          <template #default="{ row }">
            <el-progress :percentage="quotaPercent(row.used, row.quota)" :status="row.used >= row.quota ? 'warning' : undefined" />
            <div class="muted">{{ row.used }} / {{ row.quota }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" plain @click="publishNow(row)">立即发布</el-button>
            <el-button size="small" type="danger" plain @click="cancel(row)">移出队列</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, ref } from "vue";

import { api } from "@/api";
import type { Account, PublishQueueItem } from "@/types";
import { formatTime, shortText } from "@/utils";

const accounts = ref<Account[]>([]);
const items = ref<PublishQueueItem[]>([]);
const accountKey = ref("");
const loading = ref(false);

function accountName(key: string) {
  return accounts.value.find((item) => item.account_key === key)?.name || key;
}

function quotaPercent(used: number, quota: number) {
  return quota ? Math.min(100, Math.round((used / quota) * 100)) : 0;
}

async function loadQueue() {
  loading.value = true;
  try {
    items.value = (await api.publishQueue(accountKey.value || undefined)).items;
  } finally {
    loading.value = false;
  }
}

async function publishNow(row: PublishQueueItem) {
  let ignoreQuota = false;
  if (row.used >= row.quota) {
    await ElMessageBox.confirm("该账号今日已达配额，是否人工强制发布？", "配额已满", {
      type: "warning",
      confirmButtonText: "强制发布",
    });
    ignoreQuota = true;
  }
  const result = await api.publishQueueItemNow(row.generated_id, ignoreQuota);
  await loadQueue();
  if (result.success) ElMessage.success("发布成功");
  else ElMessage.warning(result.reason === "daily_quota_reached" ? "今日配额已满" : "发布未成功，请查看历史");
}

async function cancel(row: PublishQueueItem) {
  await ElMessageBox.confirm("确认把这篇稿件移出发布队列？", "移出队列", { type: "warning" });
  await api.cancelPublishQueueItem(row.generated_id);
  await loadQueue();
  ElMessage.success("已移出发布队列");
}

onMounted(async () => {
  accounts.value = await api.accounts();
  await loadQueue();
});
</script>

<style scoped>
.queue-page {
  display: grid;
  gap: 16px;
}

.cell-title {
  color: var(--text);
  font-weight: 600;
}
</style>
