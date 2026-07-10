<template>
  <el-card class="page-card" shadow="never">
    <template #header>
      <div class="toolbar">
        <div class="toolbar-title">
          <strong>系统设置</strong>
          <span>大模型、发布、邮件预警、自动运行参数</span>
        </div>
        <el-space wrap>
          <el-button v-if="activeTab === 'llm'" plain :loading="testingLlm" @click="testLlm">测试 LLM</el-button>
          <el-button v-if="activeTab === 'llm'" plain :loading="testingEmbedding" @click="testEmbedding">测试 Embedding</el-button>
          <el-button plain @click="loadSettings">刷新</el-button>
        </el-space>
      </div>
    </template>

    <el-tabs v-model="activeTab" @tab-change="syncRoute">
      <el-tab-pane label="大模型设置" name="llm">
        <el-form :model="form" label-width="170px" class="settings-form">
          <el-form-item label="LLM API Key" class="wide">
            <el-input v-model="form.llm_api_key" placeholder="已保存时显示加密掩码；输入新 key 可覆盖" />
            <div class="muted">当前：{{ settings?.llm_api_key_masked || "未保存" }}</div>
          </el-form-item>
          <el-form-item label="LLM Base URL">
            <el-input v-model="form.llm_base_url" placeholder="https://..." />
          </el-form-item>
          <el-form-item label="LLM Model">
            <el-input v-model="form.llm_model" placeholder="选择或输入模型名" />
          </el-form-item>
          <el-form-item label="模型列表" class="wide">
            <el-space wrap>
              <el-button type="primary" plain :loading="fetchingModels" @click="fetchModels">获取模型</el-button>
              <el-select v-model="form.llm_model" filterable allow-create placeholder="填好 URL 和 Key 后获取模型" style="width: 360px">
                <el-option v-for="model in modelOptions" :key="model" :label="model" :value="model" />
              </el-select>
            </el-space>
          </el-form-item>
          <el-form-item label="Embedding 服务">
            <el-select v-model="form.embedding_provider" style="width: 100%" @change="onEmbeddingProviderChange">
              <el-option label="OpenAI 兼容（智谱等）" value="openai" />
              <el-option label="阿里云百炼 DashScope" value="dashscope" />
            </el-select>
          </el-form-item>
          <el-form-item label="Embedding Model">
            <el-input v-model="form.embedding_model" :placeholder="form.embedding_provider === 'dashscope' ? 'text-embedding-v3' : 'embedding-3'" />
          </el-form-item>
          <el-form-item label="Embedding API Key" class="wide">
            <el-input v-model="form.embedding_api_key" placeholder="OpenAI 兼容模式留空可复用 LLM API Key" />
            <div class="muted">
              当前：{{ settings?.embedding_api_key_masked || (settings?.embedding_uses_llm_credentials ? "复用 LLM API Key" : "未保存") }}
            </div>
          </el-form-item>
          <el-form-item v-if="form.embedding_provider === 'openai'" label="Embedding Base URL" class="wide">
            <el-input v-model="form.embedding_base_url" placeholder="留空可复用 LLM Base URL；智谱示例 https://open.bigmodel.cn/api/paas/v4/" />
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <el-tab-pane label="邮箱预警设置" name="alerts">
        <el-form :model="form" label-width="170px" class="settings-form">
          <el-form-item label="邮件提醒">
            <el-switch v-model="form.alert_email_enabled" />
          </el-form-item>
          <el-form-item label="连续失败提醒阈值">
            <el-input-number v-model="form.publish_failure_alert_threshold" :min="1" />
          </el-form-item>
          <el-form-item label="提醒邮箱">
            <el-input v-model="form.alert_email_to" placeholder="收到告警的邮箱" />
          </el-form-item>
          <el-form-item label="SMTP Host">
            <el-input v-model="form.smtp_host" placeholder="smtp.example.com" />
          </el-form-item>
          <el-form-item label="SMTP Port">
            <el-input-number v-model="form.smtp_port" :min="1" />
          </el-form-item>
          <el-form-item label="SMTP 用户名">
            <el-input v-model="form.smtp_username" placeholder="发件账号" />
          </el-form-item>
          <el-form-item label="SMTP 密码">
            <el-input v-model="form.smtp_password" placeholder="已保存时显示加密掩码；输入新密码可覆盖" />
          </el-form-item>
          <el-form-item label="发件邮箱">
            <el-input v-model="form.smtp_from" placeholder="默认使用 SMTP 用户名" />
          </el-form-item>
          <el-form-item label="SMTP TLS">
            <el-switch v-model="form.smtp_use_tls" />
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <el-tab-pane label="自动运行设置" name="runtime">
        <el-form :model="form" label-width="170px" class="settings-form">
          <el-form-item label="默认 MCP 地址" class="wide">
            <el-input v-model="form.mcp_url" placeholder="https://your-mcp.example.com/mcp" />
          </el-form-item>
          <el-form-item label="默认发布工具">
            <el-input v-model="form.mcp_publish_tool" placeholder="publish_binance_square" />
          </el-form-item>
          <el-form-item label="默认 MCP Token" class="wide">
            <el-input v-model="form.mcp_auth_token" placeholder="已保存时显示加密掩码；输入新 token 可覆盖" />
            <div class="muted">当前：{{ settings?.mcp_auth_token_masked || "未保存" }}</div>
          </el-form-item>
          <el-form-item label="自动循环">
            <el-switch v-model="form.auto_monitor_enabled" />
          </el-form-item>
          <el-form-item label="自动发布">
            <el-switch v-model="form.auto_publish" />
          </el-form-item>
          <el-form-item label="自动消费素材">
            <el-switch v-model="form.auto_consume_materials" />
          </el-form-item>
          <el-form-item label="采集间隔 秒">
            <el-input-number v-model="form.material_poll_interval_seconds" :min="30" />
          </el-form-item>
          <el-form-item label="成功后间隔 秒">
            <el-input-number v-model="form.material_success_interval_seconds" :min="60" />
          </el-form-item>
          <el-form-item label="失败后重试 秒">
            <el-input-number v-model="form.material_failure_interval_seconds" :min="30" />
          </el-form-item>
          <el-form-item label="素材有效期 秒">
            <el-input-number v-model="form.material_ttl_seconds" :min="60" />
          </el-form-item>
          <el-form-item label="每轮消费数量">
            <el-input-number v-model="form.material_consume_batch_size" :min="1" />
          </el-form-item>
        </el-form>
      </el-tab-pane>
    </el-tabs>

    <div class="settings-actions">
      <el-button type="primary" :loading="saving" @click="saveSettings">保存配置</el-button>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ElMessage } from "element-plus";
import { onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api } from "@/api";
import type { Settings } from "@/types";

type SettingsTab = "llm" | "alerts" | "runtime";

const route = useRoute();
const router = useRouter();
const activeTab = ref<SettingsTab>((route.query.tab as SettingsTab) || "llm");
const settings = ref<Settings | null>(null);
const modelOptions = ref<string[]>([]);
const saving = ref(false);
const testingLlm = ref(false);
const testingEmbedding = ref(false);
const fetchingModels = ref(false);

const form = reactive<Record<string, any>>({
  llm_api_key: "",
  llm_base_url: "",
  llm_model: "",
  embedding_provider: "openai",
  embedding_api_key: "",
  embedding_base_url: "",
  embedding_model: "embedding-3",
  mcp_url: "",
  mcp_publish_tool: "publish_binance_square",
  mcp_auth_token: "",
  auto_monitor_enabled: true,
  auto_publish: true,
  auto_consume_materials: true,
  material_poll_interval_seconds: 300,
  material_success_interval_seconds: 600,
  material_failure_interval_seconds: 120,
  material_ttl_seconds: 7200,
  material_consume_batch_size: 1,
  publish_failure_alert_threshold: 5,
  alert_email_enabled: false,
  alert_email_to: "",
  smtp_host: "",
  smtp_port: 587,
  smtp_username: "",
  smtp_password: "",
  smtp_from: "",
  smtp_use_tls: true,
});

function isMasked(value: string) {
  return value.includes("*") || value.includes("•");
}

function applySettings(data: Settings) {
  settings.value = data;
  form.llm_api_key = data.llm_api_key_masked || "";
  form.llm_base_url = data.llm_base_url || "";
  form.llm_model = data.llm_model || "";
  form.embedding_provider = data.embedding_provider || "openai";
  form.embedding_api_key = data.embedding_api_key_masked || "";
  form.embedding_base_url = data.embedding_base_url || "";
  form.embedding_model = data.embedding_model || (form.embedding_provider === "dashscope" ? "text-embedding-v3" : "embedding-3");
  form.mcp_url = data.mcp_url || "";
  form.mcp_publish_tool = data.mcp_publish_tool || "publish_binance_square";
  form.mcp_auth_token = data.mcp_auth_token_masked || "";
  form.auto_monitor_enabled = Boolean(data.auto_monitor_enabled);
  form.auto_publish = Boolean(data.auto_publish);
  form.auto_consume_materials = Boolean(data.auto_consume_materials);
  form.material_poll_interval_seconds = data.material_poll_interval_seconds || 300;
  form.material_success_interval_seconds = data.material_success_interval_seconds || 600;
  form.material_failure_interval_seconds = data.material_failure_interval_seconds || 120;
  form.material_ttl_seconds = data.material_ttl_seconds || 7200;
  form.material_consume_batch_size = data.material_consume_batch_size || 1;
  form.publish_failure_alert_threshold = data.publish_failure_alert_threshold || 5;
  form.alert_email_enabled = Boolean(data.alert_email_enabled);
  form.alert_email_to = data.alert_email_to || "";
  form.smtp_host = data.smtp_host || "";
  form.smtp_port = data.smtp_port || 587;
  form.smtp_username = data.smtp_username || "";
  form.smtp_password = data.smtp_password_masked || "";
  form.smtp_from = data.smtp_from || "";
  form.smtp_use_tls = data.smtp_use_tls !== false;
  modelOptions.value = data.llm_model_options || (data.llm_model ? [data.llm_model] : []);
}

async function loadSettings() {
  applySettings(await api.settings());
}

function payload() {
  return {
    llm_api_key: form.llm_api_key && !isMasked(form.llm_api_key) ? form.llm_api_key : null,
    llm_base_url: form.llm_base_url,
    llm_model: form.llm_model,
    embedding_provider: form.embedding_provider,
    embedding_api_key:
      form.embedding_api_key && !isMasked(form.embedding_api_key) ? form.embedding_api_key : null,
    embedding_base_url: form.embedding_base_url,
    embedding_model: form.embedding_model,
    mcp_url: form.mcp_url,
    mcp_publish_tool: form.mcp_publish_tool,
    mcp_auth_token:
      form.mcp_auth_token && !isMasked(form.mcp_auth_token) ? form.mcp_auth_token : null,
    auto_monitor_enabled: form.auto_monitor_enabled,
    auto_publish: form.auto_publish,
    auto_consume_materials: form.auto_consume_materials,
    material_poll_interval_seconds: form.material_poll_interval_seconds,
    material_success_interval_seconds: form.material_success_interval_seconds,
    material_failure_interval_seconds: form.material_failure_interval_seconds,
    material_ttl_seconds: form.material_ttl_seconds,
    material_consume_batch_size: form.material_consume_batch_size,
    publish_failure_alert_threshold: form.publish_failure_alert_threshold,
    alert_email_enabled: form.alert_email_enabled,
    alert_email_to: form.alert_email_to,
    smtp_host: form.smtp_host,
    smtp_port: form.smtp_port,
    smtp_username: form.smtp_username,
    smtp_password: form.smtp_password && !isMasked(form.smtp_password) ? form.smtp_password : null,
    smtp_from: form.smtp_from,
    smtp_use_tls: form.smtp_use_tls,
  };
}

async function saveSettings() {
  saving.value = true;
  try {
    await api.saveSettings(payload());
    await loadSettings();
    ElMessage.success("配置已保存");
  } finally {
    saving.value = false;
  }
}

async function testLlm() {
  testingLlm.value = true;
  try {
    await saveSettings();
    const result = await api.testLlm();
    if (result.ok) ElMessage.success(result.message);
    else ElMessage.error(result.message);
  } finally {
    testingLlm.value = false;
  }
}

async function testEmbedding() {
  testingEmbedding.value = true;
  try {
    await saveSettings();
    const result = await api.testEmbedding();
    if (result.ok) ElMessage.success(result.message);
    else ElMessage.error(result.message);
  } finally {
    testingEmbedding.value = false;
  }
}

async function fetchModels() {
  fetchingModels.value = true;
  try {
    await saveSettings();
    const result = await api.models();
    modelOptions.value = result.models || [];
    if (!form.llm_model && modelOptions.value[0]) form.llm_model = modelOptions.value[0];
    ElMessage.success(`已获取 ${modelOptions.value.length} 个模型`);
  } finally {
    fetchingModels.value = false;
  }
}

function onEmbeddingProviderChange(provider: string) {
  if (provider === "openai" && form.embedding_model === "text-embedding-v3") {
    form.embedding_model = "embedding-3";
  }
  if (provider === "dashscope" && form.embedding_model === "embedding-3") {
    form.embedding_model = "text-embedding-v3";
  }
}

function syncRoute() {
  router.replace({ path: "/settings", query: { tab: activeTab.value } });
}

watch(
  () => route.query.tab,
  (tab) => {
    activeTab.value = (tab as SettingsTab) || "llm";
  },
);

onMounted(loadSettings);
</script>

<style scoped>
.settings-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2px 18px;
  max-width: 1100px;
}

.settings-form .wide {
  grid-column: 1 / -1;
}

.settings-actions {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--border-soft);
}

@media (max-width: 760px) {
  .settings-form {
    grid-template-columns: 1fr;
  }

  .settings-form .wide {
    grid-column: auto;
  }
}
</style>
