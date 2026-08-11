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
      <el-form-item label="发布前人工审核">
        <el-switch v-model="form.require_manual_review" />
        <span class="muted form-hint">关闭后仅 Gate 完全通过的稿件会自动进发布队列</span>
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
      <el-table-column label="操作" width="340" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="checkAccount(row.account_key)">检测</el-button>
          <el-button size="small" type="primary" plain @click="openProfile(row)">风格档案</el-button>
          <el-button size="small" plain :loading="loadingAccountKey === row.account_key" @click="editAccount(row.account_key)">
            编辑
          </el-button>
          <el-button size="small" type="danger" plain @click="deleteAccount(row.account_key)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer
      v-model="profileDrawerVisible"
      :title="`${profileAccountName || profileAccountKey} · 风格档案`"
      size="min(720px, 92vw)"
    >
      <div v-loading="profileLoading" class="profile-drawer">
        <el-alert
          v-if="!profileSummary?.profile"
          title="尚未生成风格档案"
          description="先导入历史文章，再点击“重新构建档案”。构建会逐篇分析，并重建该账号的风格检索库。"
          type="warning"
          :closable="false"
          show-icon
        />

        <template v-else>
          <el-descriptions :column="1" border class="profile-section">
            <el-descriptions-item label="人设定位">
              {{ profileSummary.profile.profile.persona }}
            </el-descriptions-item>
            <el-descriptions-item label="语气">
              {{ profileSummary.profile.profile.tone }}
            </el-descriptions-item>
            <el-descriptions-item label="风险偏好">
              <el-tag type="warning" effect="plain">
                {{ profileSummary.profile.profile.risk_level }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="开场方式">
              {{ profileSummary.profile.profile.opening_style }}
            </el-descriptions-item>
            <el-descriptions-item label="常写主题">
              <el-space wrap>
                <el-tag
                  v-for="item in profileSummary.profile.profile.favorite_topics"
                  :key="item"
                  effect="plain"
                >{{ item }}</el-tag>
              </el-space>
            </el-descriptions-item>
            <el-descriptions-item label="常用词">
              {{ profileSummary.profile.profile.favorite_words.join("、") || "暂无" }}
            </el-descriptions-item>
            <el-descriptions-item label="稳定观点">
              <ul class="profile-list">
                <li v-for="item in profileSummary.profile.profile.beliefs" :key="item">{{ item }}</li>
              </ul>
            </el-descriptions-item>
            <el-descriptions-item label="结构习惯">
              <ul class="profile-list">
                <li v-for="item in profileSummary.profile.profile.structure_patterns" :key="item">{{ item }}</li>
              </ul>
            </el-descriptions-item>
            <el-descriptions-item label="更新时间">
              {{ formatTime(profileSummary.profile.updated_at) }}
            </el-descriptions-item>
          </el-descriptions>
        </template>

        <el-card shadow="never" class="profile-section">
          <template #header>
            <div class="profile-section-header">
              <strong>参考文章</strong>
              <span class="muted">共 {{ profileSummary?.reference_count || 0 }} 篇</span>
            </div>
          </template>
          <el-space wrap>
            <el-tag type="info" effect="plain">待分析 {{ analysisCount("pending") }}</el-tag>
            <el-tag type="success" effect="plain">成功 {{ analysisCount("success") }}</el-tag>
            <el-tag type="danger" effect="plain">失败 {{ analysisCount("failed") }}</el-tag>
          </el-space>
        </el-card>

        <el-card shadow="never" class="profile-section">
          <template #header><strong>批量导入历史文章</strong></template>
          <el-input
            v-model="referencePostsText"
            type="textarea"
            :rows="10"
            placeholder="粘贴文章正文；多篇文章请用单独一行 --- 分隔"
          />
          <div class="profile-actions">
            <el-button :loading="referenceImporting" @click="importReferencePosts">导入</el-button>
            <el-button type="primary" :loading="profileBuilding" @click="buildProfile">
              重新构建档案
            </el-button>
          </div>
        </el-card>
      </div>
    </el-drawer>
  </el-card>
</template>

<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";

import { api } from "@/api";
import type { Account, AccountProfileSummary } from "@/types";
import { formatTime } from "@/utils";

const accounts = ref<Account[]>([]);
const saving = ref(false);
const editingAccountKey = ref("");
const loadingAccountKey = ref("");
const importing = ref(false);
const finishingImport = ref(false);
const COOKIE_IMPORT_SESSION_KEY = "bn_square_cookie_import_session";
const cookieImportSessionId = ref(sessionStorage.getItem(COOKIE_IMPORT_SESSION_KEY) || "");
const showAdvanced = ref(false);
const profileDrawerVisible = ref(false);
const profileLoading = ref(false);
const profileBuilding = ref(false);
const referenceImporting = ref(false);
const profileAccountKey = ref("");
const profileAccountName = ref("");
const profileSummary = ref<AccountProfileSummary | null>(null);
const referencePostsText = ref("");
const form = reactive({
  account_key: "",
  name: "",
  cookie: "",
  proxy_url: "",
  mcp_url: "",
  mcp_auth_token: "",
  require_manual_review: true,
});

function setCookieImportSessionId(sessionId: string) {
  cookieImportSessionId.value = sessionId;
  if (sessionId) {
    sessionStorage.setItem(COOKIE_IMPORT_SESSION_KEY, sessionId);
  } else {
    sessionStorage.removeItem(COOKIE_IMPORT_SESSION_KEY);
  }
}

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
  form.require_manual_review = true;
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
      require_manual_review: form.require_manual_review,
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
    setCookieImportSessionId(result.session_id);
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
    setCookieImportSessionId("");
    editingAccountKey.value = "";
    resetForm();
    await loadAccounts();
    ElMessage.success(`Cookie 已导入：${result.cookie_length} 字符`);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Cookie 导入验证失败";
    if (message.includes("导入会话不存在或已结束")) {
      setCookieImportSessionId("");
    }
    ElMessage.error({ message, duration: 8000, showClose: true });
  } finally {
    finishingImport.value = false;
  }
}

async function cancelCookieImport() {
  if (!cookieImportSessionId.value) return;
  await api.cancelCookieImport(cookieImportSessionId.value);
  setCookieImportSessionId("");
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
    form.require_manual_review = detail.require_manual_review;
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

function analysisCount(status: string) {
  return profileSummary.value?.analysis_status?.[status] || 0;
}

async function loadProfile() {
  if (!profileAccountKey.value) return;
  profileLoading.value = true;
  try {
    profileSummary.value = await api.accountProfile(profileAccountKey.value);
  } finally {
    profileLoading.value = false;
  }
}

async function openProfile(account: Account) {
  profileAccountKey.value = account.account_key;
  profileAccountName.value = account.name;
  profileSummary.value = null;
  referencePostsText.value = "";
  profileDrawerVisible.value = true;
  await loadProfile();
}

async function importReferencePosts() {
  const posts = referencePostsText.value
    .split(/\n\s*---\s*\n/g)
    .map((content) => content.trim())
    .filter(Boolean)
    .map((content) => ({ content }));
  if (!posts.length) {
    ElMessage.warning("请先粘贴至少一篇历史文章");
    return;
  }
  referenceImporting.value = true;
  try {
    const result = await api.importReferencePosts(profileAccountKey.value, posts);
    referencePostsText.value = "";
    await loadProfile();
    ElMessage.success(`已导入 ${result.added} 篇，跳过重复 ${result.duplicated} 篇`);
  } finally {
    referenceImporting.value = false;
  }
}

async function buildProfile() {
  profileBuilding.value = true;
  try {
    const result = await api.buildAccountProfile(profileAccountKey.value);
    await loadProfile();
    ElMessage.success(
      `档案构建完成：本次成功 ${result.analyzed_count} 篇，失败 ${result.failed_count} 篇`,
    );
  } finally {
    profileBuilding.value = false;
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

.advanced-toggle {
  align-items: center;
}

.form-hint {
  margin-left: 10px;
}

.advanced-block {
  display: contents;
}

.data-table {
  margin-top: 14px;
}

.profile-drawer {
  min-height: 240px;
}

.profile-section {
  margin-top: 16px;
}

.profile-section-header,
.profile-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.profile-actions {
  justify-content: flex-end;
  margin-top: 14px;
}

.profile-list {
  margin: 0;
  padding-left: 20px;
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
