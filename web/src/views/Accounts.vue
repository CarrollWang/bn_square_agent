<template>
  <el-card class="page-card" shadow="never">
    <template #header>
        <div class="toolbar">
          <div class="toolbar-title">
            <strong>账号管理</strong>
          <span>系统建议长期运行在服务器，本地只在需要更新 Cookie 时临时参与</span>
          </div>
          <el-button plain @click="loadAccounts">刷新</el-button>
        </div>
      </template>

      <el-alert
        title="推荐模式：服务器长期运行，本机只在 Cookie 失效时临时更新。"
        description="“打开登录窗口导入 Cookie”会在当前服务所在机器打开浏览器。部署到无头服务器时，建议在本机临时运行同版本导入，或者直接把 Cookie 粘贴到服务器后台。编辑已有账号时，Cookie 和独立 MCP Token 留空会保留已保存值。"
        type="info"
        :closable="false"
        show-icon
        class="form-alert"
      />

    <el-form :model="form" label-width="140px" class="form-grid">
      <el-form-item label="账号标识">
        <el-input v-model="form.account_key" :disabled="Boolean(editingAccountKey)" placeholder="acc_1" />
      </el-form-item>
      <el-form-item label="显示名称">
        <el-input v-model="form.name" placeholder="账号 1" />
      </el-form-item>
      <el-form-item label="独立代理" class="wide">
        <el-input
          v-model="form.proxy_url"
          placeholder="http://user:pass@host:port 或 socks5://host:port"
        />
      </el-form-item>
      <el-form-item class="wide advanced-toggle">
        <el-checkbox v-model="showAdvanced">高级发布通道配置</el-checkbox>
        <span class="muted">默认沿用全局 MCP 设置，通常不需要单独填写</span>
      </el-form-item>
      <el-collapse-transition>
        <div v-show="showAdvanced" class="advanced-block">
          <el-form-item label="独立 MCP 地址" class="wide">
            <el-input v-model="form.mcp_url" placeholder="留空则沿用全局 MCP_URL" />
          </el-form-item>
          <el-form-item label="独立 MCP Token" class="wide">
            <el-input v-model="form.mcp_auth_token" placeholder="留空则沿用已保存值或全局 Token" />
          </el-form-item>
        </div>
      </el-collapse-transition>
      <el-form-item label="Binance Cookie" class="wide">
        <el-input
          v-model="form.cookie"
          type="textarea"
          :rows="6"
          :placeholder="editingAccountKey ? '留空则保留已保存 Cookie；输入新 Cookie 可覆盖' : '粘贴浏览器 Cookie'"
        />
      </el-form-item>
      <el-form-item class="wide">
        <el-button type="primary" :loading="saving" @click="saveAccount">保存账号</el-button>
        <el-button :loading="importing" @click="startCookieImport">在当前机器打开登录窗口导入 Cookie</el-button>
        <el-button
          v-if="cookieImportSessionId"
          type="success"
          :loading="finishingImport"
          @click="finishCookieImport"
        >
          完成导入
        </el-button>
        <el-button v-if="cookieImportSessionId" plain @click="cancelCookieImport">取消导入</el-button>
        <el-button v-if="editingAccountKey" plain @click="resetForm">取消编辑</el-button>
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
      <el-table-column label="隔离网络" min-width="220">
        <template #default="{ row }">
          <el-tag :type="row.proxy_configured ? 'warning' : 'info'" effect="plain">
            {{ row.proxy_configured ? "独立代理" : "默认出口" }}
          </el-tag>
          <div class="muted">{{ row.proxy_url_masked || "未配置" }}</div>
        </template>
      </el-table-column>
      <el-table-column label="发布通道" min-width="260">
        <template #default="{ row }">
          <div>{{ row.mcp_url || "沿用全局 MCP_URL" }}</div>
          <div class="muted">
            {{ row.mcp_auth_token_configured ? "账号独立 Token 已保存" : "沿用全局 Token / 无 Token" }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="检测" width="170">
        <template #default="{ row }">
          <div>{{ row.check_status || "unchecked" }}</div>
          <div class="muted">{{ formatTime(row.checked_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="250" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="checkAccount(row.account_key)">检测</el-button>
          <el-button size="small" plain :loading="loadingAccountKey === row.account_key" @click="editAccount(row.account_key)">
            编辑
          </el-button>
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
const editingAccountKey = ref("");
const loadingAccountKey = ref("");
const importing = ref(false);
const finishingImport = ref(false);
const cookieImportSessionId = ref("");
const showAdvanced = ref(false);
const form = reactive({
  account_key: "",
  name: "",
  cookie: "",
  proxy_url: "",
  mcp_url: "",
  mcp_auth_token: "",
});

async function loadAccounts() {
  accounts.value = await api.accounts();
}

function resetForm() {
  if (cookieImportSessionId.value) {
    ElMessage.warning("请先完成或取消当前 Cookie 导入")
    return;
  }
  editingAccountKey.value = "";
  form.account_key = "";
  form.name = "";
  form.cookie = "";
  form.proxy_url = "";
  form.mcp_url = "";
  form.mcp_auth_token = "";
  showAdvanced.value = false;
}

async function saveAccount() {
  saving.value = true;
  try {
    await api.saveAccount({
      account_key: form.account_key.trim(),
      name: form.name.trim(),
      cookie: form.cookie.trim() || null,
      proxy_url: form.proxy_url.trim(),
      mcp_url: form.mcp_url.trim(),
      mcp_auth_token: form.mcp_auth_token.trim() || null,
    });
    resetForm();
    await loadAccounts();
    ElMessage.success("账号已保存");
  } finally {
    saving.value = false;
  }
}

async function startCookieImport() {
  if (cookieImportSessionId.value) {
    ElMessage.warning("当前已有一个 Cookie 导入会话，请先完成或取消")
    return;
  }
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
    editingAccountKey.value = "";
    resetForm();
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

async function editAccount(accountKey: string) {
  if (cookieImportSessionId.value) {
    ElMessage.warning("请先完成或取消当前 Cookie 导入")
    return;
  }
  loadingAccountKey.value = accountKey;
  try {
    const detail = await api.account(accountKey);
    editingAccountKey.value = detail.account_key;
    form.account_key = detail.account_key;
    form.name = detail.name || "";
    form.cookie = "";
    form.proxy_url = detail.proxy_url || "";
    form.mcp_url = detail.mcp_url || "";
    form.mcp_auth_token = "";
    showAdvanced.value = Boolean(detail.mcp_url || detail.mcp_auth_token_configured);
  } finally {
    loadingAccountKey.value = "";
  }
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

.form-alert {
  margin-bottom: 16px;
}

.form-grid .wide {
  grid-column: 1 / -1;
}

.advanced-toggle {
  align-items: center;
}

.advanced-block {
  display: contents;
}

.data-table {
  margin-top: 14px;
}

@media (max-width: 760px) {
  .form-grid {
    grid-template-columns: 1fr;
  }

  .form-grid .wide {
    grid-column: auto;
  }
}
</style>
