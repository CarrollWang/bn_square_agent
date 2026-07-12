<template>
  <el-card class="page-card" shadow="never">
    <template #header>
      <div class="toolbar">
        <div class="toolbar-title">
          <strong>账号管理</strong>
          <span>每个账号使用独立的 Binance Square OpenAPI Key，自建 MCP 负责轮转与发布回写</span>
        </div>
        <el-button plain @click="loadAccounts">刷新</el-button>
      </div>
    </template>

    <el-alert
      title="只使用 Binance 官方 Square OpenAPI"
      description="不再使用 Cookie、扫码登录或浏览器发布。Key 只会加密保存到本机数据库，账号列表和接口不会返回明文。"
      type="success"
      :closable="false"
      show-icon
      class="form-alert"
    />

    <el-form :model="form" label-width="160px" class="form-grid">
      <el-form-item label="账号标识">
        <el-input v-model="form.account_key" :disabled="Boolean(editingAccountKey)" placeholder="acc_1" />
      </el-form-item>
      <el-form-item label="显示名称">
        <el-input v-model="form.name" placeholder="账号 1" />
      </el-form-item>
      <el-form-item label="Square OpenAPI Key" class="wide">
        <el-input
          v-model="form.square_openapi_key"
          type="password"
          show-password
          autocomplete="new-password"
          :placeholder="editingAccountKey ? '留空则保留已保存 Key；输入新 Key 可覆盖' : '粘贴 Binance Square Creator Center 创建的 Key'"
        />
        <div class="muted key-help">
          创建入口：
          <a href="https://www.binance.com/square/creator-center/home" target="_blank" rel="noreferrer">
            Binance Square Creator Center
          </a>
        </div>
      </el-form-item>
      <el-form-item label="独立代理" class="wide">
        <el-input
          v-model="form.proxy_url"
          placeholder="可选：http://user:pass@host:port 或 socks5://host:port"
        />
      </el-form-item>
      <el-form-item class="wide advanced-toggle">
        <el-checkbox v-model="showAdvanced">高级 MCP 通道配置</el-checkbox>
        <span class="muted">默认沿用全局 MCP 设置</span>
      </el-form-item>
      <el-collapse-transition>
        <div v-show="showAdvanced" class="advanced-block">
          <el-form-item label="独立 MCP 地址" class="wide">
            <el-input v-model="form.mcp_url" placeholder="留空则沿用全局 MCP_URL" />
          </el-form-item>
          <el-form-item label="独立 MCP Token" class="wide">
            <el-input
              v-model="form.mcp_auth_token"
              type="password"
              show-password
              placeholder="留空则沿用已保存值或全局 Token"
            />
          </el-form-item>
        </div>
      </el-collapse-transition>
      <el-form-item class="wide">
        <el-button type="primary" :loading="saving" @click="saveAccount">保存账号</el-button>
        <el-button v-if="editingAccountKey" plain @click="resetForm">取消编辑</el-button>
      </el-form-item>
    </el-form>

    <el-table :data="accounts" border stripe class="data-table">
      <el-table-column prop="name" label="账号" min-width="150">
        <template #default="{ row }">
          <strong>{{ row.name || row.account_key }}</strong>
          <div class="muted">key: {{ row.account_key }}</div>
        </template>
      </el-table-column>
      <el-table-column label="Square OpenAPI" min-width="180">
        <template #default="{ row }">
          <el-tag :type="row.square_openapi_key_configured ? 'success' : 'danger'" effect="plain">
            {{ row.square_openapi_key_configured ? "已配置" : "缺失" }}
          </el-tag>
          <div class="muted">明文不会通过接口返回</div>
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
      <el-table-column label="配置状态" width="150">
        <template #default="{ row }">
          <div>{{ row.check_status || (row.square_openapi_key_configured ? "configured" : "missing") }}</div>
          <div class="muted">{{ formatTime(row.checked_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="260" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="checkAccount(row.account_key)">检查配置</el-button>
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
const showAdvanced = ref(false);
const form = reactive({
  account_key: "",
  name: "",
  square_openapi_key: "",
  proxy_url: "",
  mcp_url: "",
  mcp_auth_token: "",
});

async function loadAccounts() {
  accounts.value = await api.accounts();
}

function resetForm() {
  editingAccountKey.value = "";
  form.account_key = "";
  form.name = "";
  form.square_openapi_key = "";
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
      square_openapi_key: form.square_openapi_key.trim() || null,
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

async function editAccount(accountKey: string) {
  loadingAccountKey.value = accountKey;
  try {
    const detail = await api.account(accountKey);
    editingAccountKey.value = detail.account_key;
    form.account_key = detail.account_key;
    form.name = detail.name || "";
    form.square_openapi_key = "";
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
  if (result.configured) {
    ElMessage.success("Square OpenAPI Key 已配置");
  } else {
    ElMessage.error(result.error || "Square OpenAPI Key 缺失");
  }
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

.key-help {
  margin-top: 6px;
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
