<template>
  <el-card class="page-card" shadow="never">
    <template #header>
      <div class="toolbar">
        <div class="toolbar-title">
          <strong>账号管理</strong>
          <span>保存 Binance Cookie，多账号自动发文</span>
        </div>
        <el-button plain @click="loadAccounts">刷新</el-button>
      </div>
    </template>

    <el-form :model="form" label-width="120px" class="form-grid">
      <el-form-item label="账号标识">
        <el-input v-model="form.account_key" placeholder="acc_1" />
      </el-form-item>
      <el-form-item label="显示名称">
        <el-input v-model="form.name" placeholder="账号 1" />
      </el-form-item>
      <el-form-item label="Binance Cookie" class="wide">
        <el-input v-model="form.cookie" type="textarea" :rows="6" placeholder="粘贴浏览器 Cookie" />
      </el-form-item>
      <el-form-item class="wide">
        <el-button type="primary" :loading="saving" @click="saveAccount">保存账号</el-button>
        <el-button :loading="importing" @click="startCookieImport">打开登录窗口导入 Cookie</el-button>
        <el-button v-if="cookieImportSessionId" type="success" :loading="finishingImport" @click="finishCookieImport">
          完成导入
        </el-button>
        <el-button v-if="cookieImportSessionId" plain @click="cancelCookieImport">取消导入</el-button>
      </el-form-item>
    </el-form>

    <el-table :data="accounts" border stripe class="data-table">
      <el-table-column prop="name" label="账号" min-width="140">
        <template #default="{ row }">
          <strong>{{ row.name || row.account_key }}</strong>
          <div class="muted">key: {{ row.account_key }}</div>
        </template>
      </el-table-column>
      <el-table-column label="Cookie" min-width="220">
        <template #default="{ row }">
          <el-tag :type="row.cookie_saved ? 'success' : 'danger'" effect="plain">
            {{ row.cookie_saved ? "已保存" : "缺失" }}
          </el-tag>
          <div class="muted">{{ row.cookie_length }} 字符</div>
          <div class="muted">{{ (row.cookie_names || []).slice(0, 6).join(", ") || "无" }}</div>
        </template>
      </el-table-column>
      <el-table-column label="检测" width="170">
        <template #default="{ row }">
          <div>{{ row.check_status || "unchecked" }}</div>
          <div class="muted">{{ formatTime(row.checked_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="checkAccount(row.account_key)">检测</el-button>
          <el-button size="small" type="danger" plain @click="deleteAccount(row.account_key)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>

<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { api } from "@/api";
import type { Account } from "@/types";
import { formatTime } from "@/utils";

const accounts = ref<Account[]>([]);
const saving = ref(false);
const importing = ref(false);
const finishingImport = ref(false);
const cookieImportSessionId = ref("");
const form = reactive({
  account_key: "",
  name: "",
  cookie: "",
});

async function loadAccounts() {
  accounts.value = await api.accounts();
}

async function saveAccount() {
  saving.value = true;
  try {
    await api.saveAccount({
      account_key: form.account_key.trim(),
      name: form.name.trim(),
      cookie: form.cookie.trim(),
    });
    form.account_key = "";
    form.name = "";
    form.cookie = "";
    await loadAccounts();
    ElMessage.success("账号已保存");
  } finally {
    saving.value = false;
  }
}

async function startCookieImport() {
  const accountKey = form.account_key.trim();
  if (!accountKey) {
    ElMessage.warning("请先填写账号标识");
    return;
  }
  importing.value = true;
  try {
    const result = await api.startCookieImport({
      account_key: accountKey,
      name: form.name.trim(),
    });
    cookieImportSessionId.value = result.session_id;
    ElMessage.success(result.message);
  } finally {
    importing.value = false;
  }
}

async function finishCookieImport() {
  if (!cookieImportSessionId.value) return;
  finishingImport.value = true;
  try {
    const result = await api.finishCookieImport(cookieImportSessionId.value);
    cookieImportSessionId.value = "";
    form.account_key = "";
    form.name = "";
    form.cookie = "";
    await loadAccounts();
    ElMessage.success(`Cookie 已导入：${result.cookie_length} 字符`);
  } finally {
    finishingImport.value = false;
  }
}

async function cancelCookieImport() {
  if (!cookieImportSessionId.value) return;
  await api.cancelCookieImport(cookieImportSessionId.value);
  cookieImportSessionId.value = "";
  ElMessage.success("已取消导入");
}

async function checkAccount(accountKey: string) {
  const result = await api.checkAccount(accountKey);
  await loadAccounts();
  ElMessage.success(result.valid ? "账号有效" : "检测完成，请查看状态");
}

async function deleteAccount(accountKey: string) {
  await ElMessageBox.confirm(`确认删除账号 ${accountKey}？`, "删除账号", { type: "warning" });
  await api.deleteAccount(accountKey);
  await loadAccounts();
  ElMessage.success("账号已删除");
}

onMounted(loadAccounts);
</script>

<style scoped>
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2px 16px;
  max-width: 1040px;
}

.form-grid .wide {
  grid-column: 1 / -1;
}

.data-table {
  margin-top: 14px;
}
</style>
