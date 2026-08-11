<template>
  <div class="review-page">
    <div class="toolbar">
      <div class="toolbar-title">
        <strong>待审核</strong>
        <span>集中检查 Gate、审核分数和最终正文，通过后进入定时发布队列</span>
      </div>
      <el-space wrap>
        <el-select v-model="accountKey" clearable placeholder="全部账号" style="width: 180px" @change="loadItems">
          <el-option v-for="item in accounts" :key="item.account_key" :label="item.name" :value="item.account_key" />
        </el-select>
        <el-button
          type="primary"
          :disabled="!selectedIds.length"
          :loading="batchApproving"
          @click="batchApprove"
        >批量通过（{{ selectedIds.length }}）</el-button>
        <el-button plain :loading="loading" @click="loadItems">刷新</el-button>
      </el-space>
    </div>

    <el-card class="page-card" shadow="never">
      <el-table :data="items" border stripe @selection-change="onSelectionChange">
        <el-table-column type="selection" width="46" />
        <el-table-column label="账号" width="150">
          <template #default="{ row }"><strong>{{ accountName(row.account_key) }}</strong></template>
        </el-table-column>
        <el-table-column label="素材" min-width="240">
          <template #default="{ row }">
            <div class="cell-title">{{ row.material_title || `material#${row.material_item_id || '-'}` }}</div>
            <div class="muted">{{ row.source_name || "手动输入" }}</div>
            <el-link v-if="row.material_url" :href="row.material_url" target="_blank" type="primary">原文</el-link>
          </template>
        </el-table-column>
        <el-table-column label="稿件" min-width="320">
          <template #default="{ row }">
            <el-popover placement="top" :width="480" trigger="hover">
              <template #reference><div class="content-preview">{{ shortText(row.content, 150) }}</div></template>
              <div class="full-content">{{ row.content }}</div>
            </el-popover>
          </template>
        </el-table-column>
        <el-table-column label="四维分" width="220">
          <template #default="{ row }">
            <el-space wrap :size="4">
              <el-tag effect="plain">事实 {{ score(row, 'factual_fidelity') }}</el-tag>
              <el-tag effect="plain">风格 {{ score(row, 'style_match') }}</el-tag>
              <el-tag effect="plain">原创 {{ score(row, 'originality') }}</el-tag>
              <el-tag effect="plain">表达 {{ score(row, 'expression_quality') }}</el-tag>
            </el-space>
          </template>
        </el-table-column>
        <el-table-column label="Gate" min-width="230">
          <template #default="{ row }">
            <el-tag :type="row.gate.status === 'ok' ? 'success' : 'warning'" effect="plain">
              {{ row.gate.status === "ok" ? "可通过" : "需人工确认" }}
            </el-tag>
            <div class="gate-reasons">
              <el-tag v-for="reason in row.gate.reasons" :key="reason" type="warning" effect="plain">
                {{ gateReasonLabel(reason) }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="210" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="success" plain @click="approve(row)">通过</el-button>
            <el-button size="small" plain @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" plain @click="reject(row)">驳回</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="editVisible" title="编辑待审核稿件" width="680px">
      <el-input v-model="editContent" type="textarea" :rows="14" />
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingEdit" @click="saveEdit">保存，需重新通过</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, ref } from "vue";

import { api } from "@/api";
import type { Account, ReviewItem } from "@/types";
import { shortText } from "@/utils";

const accounts = ref<Account[]>([]);
const items = ref<ReviewItem[]>([]);
const accountKey = ref("");
const loading = ref(false);
const batchApproving = ref(false);
const selectedIds = ref<number[]>([]);
const editVisible = ref(false);
const editId = ref(0);
const editContent = ref("");
const savingEdit = ref(false);

const reasonLabels: Record<string, string> = {
  source_url_missing: "缺少来源链接",
  direction_untagged: "方向未打标",
  source_disabled: "素材源已禁用",
  chart_image_failed: "走势图生成失败",
  review_threshold_failed: "审核阈值未通过",
};

function gateReasonLabel(reason: string) {
  return reasonLabels[reason] || reason;
}

function accountName(key: string) {
  return accounts.value.find((item) => item.account_key === key)?.name || key;
}

function score(row: ReviewItem, key: keyof NonNullable<ReviewItem["review"]["scores"]>) {
  return row.review.scores?.[key] ?? "-";
}

async function loadItems() {
  loading.value = true;
  try {
    items.value = await api.reviewItems({
      account_key: accountKey.value || undefined,
      status: "pending_review",
    });
    selectedIds.value = [];
  } finally {
    loading.value = false;
  }
}

function onSelectionChange(rows: ReviewItem[]) {
  selectedIds.value = rows.map((row) => row.generated_id);
}

async function approve(row: ReviewItem) {
  await api.approveReviewItem(row.generated_id);
  await loadItems();
  ElMessage.success("已通过并加入发布队列");
}

async function reject(row: ReviewItem) {
  const { value } = await ElMessageBox.prompt("可填写驳回原因", "驳回稿件", {
    inputType: "textarea",
    confirmButtonText: "确认驳回",
    cancelButtonText: "取消",
  });
  await api.rejectReviewItem(row.generated_id, value || "");
  await loadItems();
  ElMessage.success("已驳回，素材可重新生成");
}

function openEdit(row: ReviewItem) {
  editId.value = row.generated_id;
  editContent.value = row.content;
  editVisible.value = true;
}

async function saveEdit() {
  savingEdit.value = true;
  try {
    await api.editReviewItem(editId.value, editContent.value);
    editVisible.value = false;
    await loadItems();
    ElMessage.success("稿件已保存，仍需重新通过");
  } finally {
    savingEdit.value = false;
  }
}

async function batchApprove() {
  batchApproving.value = true;
  try {
    const result = await api.batchApproveReviewItems(selectedIds.value);
    await loadItems();
    ElMessage.success(`批量通过 ${result.approved} 条，失败 ${result.failed} 条`);
  } finally {
    batchApproving.value = false;
  }
}

onMounted(async () => {
  accounts.value = await api.accounts();
  await loadItems();
});
</script>

<style scoped>
.review-page {
  display: grid;
  gap: 16px;
}

.cell-title {
  color: var(--text);
  font-weight: 600;
}

.content-preview {
  cursor: pointer;
  line-height: 1.55;
}

.full-content {
  white-space: pre-wrap;
  line-height: 1.65;
}

.gate-reasons {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}
</style>
