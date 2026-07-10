<template>
  <div class="sources-page">
    <el-card id="source-config" class="page-card" shadow="never">
      <template #header>
        <div class="toolbar">
          <div class="toolbar-title">
            <strong>{{ isTechFlow ? "深潮源配置" : "BN 广场源配置" }}</strong>
            <span>{{ sourceTypeLabel(activeType) }}</span>
          </div>
          <el-space wrap>
            <el-button type="primary" plain :loading="checkingAll" @click="checkAll">立即采集</el-button>
            <el-button plain @click="refresh">刷新</el-button>
          </el-space>
        </div>
      </template>

      <el-alert
        v-if="isTechFlow"
        title="深潮快讯使用固定官方快讯源，无需填写来源名称和链接。"
        type="info"
        show-icon
        :closable="false"
        class="source-alert"
      />

      <el-form :model="sourceForm" label-width="110px" class="source-form">
        <el-form-item label="来源类型">
          <el-select v-model="activeType" disabled>
            <el-option label="BN 广场作者" value="binance_square" />
            <el-option label="TechFlow 深潮快讯" value="techflow_newsletter" />
          </el-select>
        </el-form-item>
        <template v-if="!isTechFlow">
          <el-form-item label="来源名称">
            <el-input v-model="sourceForm.name" placeholder="目标作者 / 频道名" />
          </el-form-item>
          <el-form-item label="素材链接" class="wide">
            <el-input v-model="sourceForm.url" placeholder="https://www.binance.com/zh-CN/square/profile/..." />
          </el-form-item>
        </template>
        <el-form-item class="wide">
          <el-button type="primary" :loading="saving" @click="saveSource">
            {{ isTechFlow ? "保存/启用深潮快讯源" : "保存 BN 广场源" }}
          </el-button>
        </el-form-item>
      </el-form>

      <el-table :data="filteredSources" border stripe class="data-table">
        <el-table-column label="来源" min-width="220">
          <template #default="{ row }">
            <strong>{{ row.name }}</strong>
            <div class="muted">{{ sourceTypeLabel(row.source_type) }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="url" label="链接" min-width="360" show-overflow-tooltip />
        <el-table-column label="上次采集" width="190">
          <template #default="{ row }">
            {{ formatTime(row.last_checked_at) }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="170">
          <template #default="{ row }">
            <el-tag :type="row.last_error ? 'danger' : 'success'" effect="plain">
              {{ row.last_error ? "异常" : "正常" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="checkSource(row.id)">采集</el-button>
            <el-button size="small" type="danger" plain @click="deleteSource(row.id)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card id="source-items" class="page-card" shadow="never">
      <template #header>
        <div class="toolbar">
          <div class="toolbar-title">
            <strong>{{ isTechFlow ? "深潮素材库" : "BN 广场素材库" }}</strong>
            <span>只展示当前来源类型的待使用素材</span>
          </div>
          <el-button plain @click="loadItems">刷新素材</el-button>
        </div>
      </template>
      <el-table :data="filteredItems" border stripe>
        <el-table-column label="素材" min-width="420">
          <template #default="{ row }">
            <strong>{{ row.title || row.source_name || `素材 #${row.id}` }}</strong>
            <p class="material-preview">{{ row.content }}</p>
          </template>
        </el-table-column>
        <el-table-column label="Tag" width="170">
          <template #default="{ row }">
            <el-tag effect="plain">{{ row.tag_status || "pending" }}</el-tag>
            <div class="muted">{{ parseTag(row.tag_json)?.symbol || "-" }}</div>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="180">
          <template #default="{ row }">
            {{ row.source_name || sourceTypeLabel(row.source_type) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="110" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" plain @click="runMaterial(row.id)">运行</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, nextTick, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { api } from "@/api";
import type { MaterialItem, MaterialSource, SourceType } from "@/types";
import { formatTime, sourceTypeLabel } from "@/utils";

const route = useRoute();
const activeType = ref<SourceType>((route.query.type as SourceType) || "binance_square");
const sources = ref<MaterialSource[]>([]);
const items = ref<MaterialItem[]>([]);
const saving = ref(false);
const checkingAll = ref(false);
const sourceForm = reactive({
  name: "",
  url: "",
});

const isTechFlow = computed(() => activeType.value === "techflow_newsletter");
const filteredSources = computed(() => sources.value.filter((item) => item.source_type === activeType.value));
const filteredItems = computed(() => items.value.filter((item) => item.source_type === activeType.value));

function parseTag(raw?: string) {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

async function loadSources() {
  sources.value = await api.materialSources();
}

async function loadItems() {
  items.value = await api.materialItems(100);
}

async function refresh() {
  await Promise.all([loadSources(), loadItems()]);
}

async function saveSource() {
  saving.value = true;
  try {
    await api.saveMaterialSource({
      name: isTechFlow.value ? "TechFlow 深潮快讯" : sourceForm.name.trim(),
      url: isTechFlow.value
        ? "https://www.techflowpost.com/newsletter?is_hot=1&articleType=1006"
        : sourceForm.url.trim(),
      source_type: activeType.value,
      enabled: true,
    });
    sourceForm.name = "";
    sourceForm.url = "";
    await loadSources();
    ElMessage.success("素材源已保存");
  } finally {
    saving.value = false;
  }
}

async function checkSource(sourceId: number) {
  const result = await api.checkMaterialSource(sourceId);
  await refresh();
  ElMessage.success(`找到 ${result.found || 0} 条，新增 ${result.inserted || 0} 条`);
}

async function checkAll() {
  checkingAll.value = true;
  try {
    if (!filteredSources.value.length && isTechFlow.value) {
      await saveSource();
    }
    const targets = filteredSources.value.length ? filteredSources.value : sources.value.filter((item) => item.source_type === activeType.value);
    if (targets.length) {
      for (const source of targets) {
        await api.checkMaterialSource(source.id);
      }
    } else {
      await api.checkMaterialSources();
    }
    await refresh();
    ElMessage.success("采集完成");
  } finally {
    checkingAll.value = false;
  }
}

async function deleteSource(sourceId: number) {
  await ElMessageBox.confirm("确认删除这个素材源？", "删除素材源", { type: "warning" });
  await api.deleteMaterialSource(sourceId);
  await loadSources();
  ElMessage.success("素材源已删除");
}

async function runMaterial(materialId: number) {
  try {
    await api.runMaterialItem(materialId);
    await loadItems();
    ElMessage.success("素材已运行");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "运行失败");
  }
}

watch(
  () => [route.query.type, route.query.section],
  async ([value, section]) => {
    activeType.value = (value as SourceType) || "binance_square";
    await nextTick();
    document
      .getElementById(section === "items" ? "source-items" : "source-config")
      ?.scrollIntoView({ block: "start" });
  },
);

onMounted(refresh);
</script>

<style scoped>
.sources-page {
  display: grid;
  gap: 16px;
}

.source-alert {
  margin-bottom: 14px;
}

.source-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2px 16px;
  max-width: 1040px;
}

.source-form .wide {
  grid-column: 1 / -1;
}

.material-preview {
  display: -webkit-box;
  max-width: 820px;
  margin: 6px 0 0;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.5;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

@media (max-width: 760px) {
  .source-form {
    grid-template-columns: 1fr;
  }

  .source-form .wide {
    grid-column: auto;
  }
}
</style>
