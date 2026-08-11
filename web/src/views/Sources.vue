<template>
  <div class="sources-page">
    <el-card id="source-config" class="page-card" shadow="never">
      <template #header>
        <div class="toolbar">
          <div class="toolbar-title">
            <strong>{{ sourceTypeLabel(activeType) }}配置</strong>
            <span>每个消息源独立采集和记录异常，单源故障不会阻断其他源</span>
          </div>
          <el-space wrap>
            <el-button type="primary" plain :loading="checkingAll" @click="checkAll">采集当前类型</el-button>
            <el-button plain @click="refresh">刷新</el-button>
          </el-space>
        </div>
      </template>

      <el-alert
        :title="activeConfig.hint"
        type="info"
        show-icon
        :closable="false"
        class="source-alert"
      />

      <el-form :model="sourceForm" label-width="110px" class="source-form">
        <el-form-item label="来源类型">
          <el-select v-model="activeType" @change="changeSourceType">
            <el-option
              v-for="option in sourceOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item v-if="activeType === 'rss_feed'" label="预设 Feed">
          <el-select v-model="rssPresetKey" @change="applyPreset">
            <el-option
              v-for="preset in rssPresets"
              :key="preset.key"
              :label="preset.name"
              :value="preset.key"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="来源名称">
          <el-input
            v-model="sourceForm.name"
            :disabled="activeType !== 'binance_square'"
            placeholder="目标作者 / 频道名"
          />
        </el-form-item>
        <el-form-item label="素材链接" class="wide">
          <el-input
            v-model="sourceForm.url"
            :disabled="activeType !== 'binance_square'"
            placeholder="https://www.binance.com/zh-CN/square/profile/..."
          />
        </el-form-item>
        <el-form-item class="wide">
          <el-button type="primary" :loading="saving" @click="saveSource">保存 / 启用消息源</el-button>
        </el-form-item>
      </el-form>

      <el-table :data="filteredSources" border stripe class="data-table">
        <el-table-column label="来源" min-width="220">
          <template #default="{ row }">
            <strong>{{ row.name }}</strong>
            <div class="muted">{{ sourceTypeLabel(row.source_type) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="链接" min-width="360">
          <template #default="{ row }">
            <el-link :href="row.url" target="_blank" type="primary">{{ row.url }}</el-link>
          </template>
        </el-table-column>
        <el-table-column label="上次采集" width="190">
          <template #default="{ row }">{{ formatTime(row.last_checked_at) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="170">
          <template #default="{ row }">
            <el-tag v-if="!row.enabled" type="info" effect="plain">已停用</el-tag>
            <el-tooltip v-else-if="row.last_error" :content="row.last_error" placement="top">
              <el-tag type="danger" effect="plain">采集异常</el-tag>
            </el-tooltip>
            <el-tag v-else-if="row.last_checked_at" type="success" effect="plain">正常</el-tag>
            <el-tag v-else type="info" effect="plain">未采集</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.enabled" size="small" @click="checkSource(row.id)">采集</el-button>
            <el-button v-else size="small" type="success" plain @click="enableSource(row)">启用</el-button>
            <el-button v-if="row.enabled" size="small" type="danger" plain @click="disableSource(row.id)">停用</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card id="source-items" class="page-card" shadow="never">
      <template #header>
        <div class="toolbar">
          <div class="toolbar-title">
            <strong>{{ sourceTypeLabel(activeType) }}素材库</strong>
            <span>只展示当前来源类型的待使用素材</span>
          </div>
          <el-button plain @click="loadItems">刷新素材</el-button>
        </div>
      </template>
      <el-table :data="items" border stripe>
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
        <el-table-column label="来源" width="190">
          <template #default="{ row }">{{ row.source_name || sourceTypeLabel(row.source_type) }}</template>
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
import { useRoute, useRouter } from "vue-router";

import { api } from "@/api";
import type { MaterialItem, MaterialSource, SourceType } from "@/types";
import { formatTime, sourceTypeLabel } from "@/utils";

interface SourcePreset {
  key: string;
  name: string;
  url: string;
  hint: string;
}

const route = useRoute();
const router = useRouter();
const sourceTypes: SourceType[] = [
  "binance_square",
  "techflow_newsletter",
  "rss_feed",
  "wallstreetcn_live",
  "chaincatcher_flash",
];
const sourceOptions = sourceTypes.map((value) => ({ value, label: sourceTypeLabel(value) }));
const rssPresets: SourcePreset[] = [
  {
    key: "wushuo",
    name: "吴说区块链",
    url: "https://www.wublock123.com/rss",
    hint: "吴说 RSS，适合作为加密行业快讯补充源。",
  },
  {
    key: "ethereum",
    name: "Ethereum Foundation Blog",
    url: "https://blog.ethereum.org/feed.xml",
    hint: "Ethereum Foundation 官方博客，属于一手项目动态。",
  },
  {
    key: "bitcoin_core",
    name: "Bitcoin Core",
    url: "https://bitcoincore.org/en/feed.xml",
    hint: "Bitcoin Core 官方发布 Feed，适合版本和协议动态。",
  },
];
const fixedPresets: Record<Exclude<SourceType, "binance_square" | "rss_feed">, SourcePreset> = {
  techflow_newsletter: {
    key: "techflow",
    name: "TechFlow 深潮快讯",
    url: "https://www.techflowpost.com/newsletter?is_hot=1&articleType=1006",
    hint: "TechFlow 官方快讯页，继续沿用现有安全解析器。",
  },
  wallstreetcn_live: {
    key: "wallstreetcn",
    name: "华尔街见闻 7×24",
    url: "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=100",
    hint: "仅保留加密市场和关键宏观事件，过滤大部分无关财经快讯。",
  },
  chaincatcher_flash: {
    key: "chaincatcher",
    name: "ChainCatcher 快讯",
    url: "https://api.chaincatcher.com/v1/open-api/news-flash?type=flash&page=1&size=50&lang=zh-CN",
    hint: "仅保留加密市场相关快讯，AI、IPO 等无关内容不会进入素材库。",
  },
};

function routeSourceType(): SourceType {
  const value = String(route.query.type || "techflow_newsletter") as SourceType;
  return sourceTypes.includes(value) ? value : "techflow_newsletter";
}

const activeType = ref<SourceType>(routeSourceType());
const rssPresetKey = ref("wushuo");
const sources = ref<MaterialSource[]>([]);
const items = ref<MaterialItem[]>([]);
const saving = ref(false);
const checkingAll = ref(false);
const sourceForm = reactive({ name: "", url: "" });

const selectedRssPreset = computed(
  () => rssPresets.find((item) => item.key === rssPresetKey.value) || rssPresets[0],
);
const activeConfig = computed<SourcePreset>(() => {
  if (activeType.value === "binance_square") {
    return {
      key: "binance_square",
      name: sourceForm.name,
      url: sourceForm.url,
      hint: "填写 BN 广场作者主页。收益大 V 只建议离线对标，不建议接入生产素材。",
    };
  }
  if (activeType.value === "rss_feed") return selectedRssPreset.value;
  return fixedPresets[activeType.value];
});
const filteredSources = computed(() =>
  sources.value.filter((item) => item.source_type === activeType.value),
);
const enabledSources = computed(() => filteredSources.value.filter((item) => Boolean(item.enabled)));

function parseTag(raw?: string) {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function applyPreset() {
  if (activeType.value === "binance_square") return;
  sourceForm.name = activeConfig.value.name;
  sourceForm.url = activeConfig.value.url;
}

async function changeSourceType() {
  applyPreset();
  await router.replace({ query: { ...route.query, type: activeType.value } });
  await loadItems();
}

async function loadSources() {
  sources.value = await api.materialSources();
}

async function loadItems() {
  items.value = await api.materialItems(100, activeType.value);
}

async function refresh() {
  await Promise.all([loadSources(), loadItems()]);
}

async function saveSource() {
  const name = sourceForm.name.trim();
  const url = sourceForm.url.trim();
  if (!name || !url) {
    ElMessage.warning("请填写来源名称和素材链接");
    return;
  }
  saving.value = true;
  try {
    await api.saveMaterialSource({
      name,
      url,
      source_type: activeType.value,
      enabled: true,
    });
    if (activeType.value === "binance_square") {
      sourceForm.name = "";
      sourceForm.url = "";
    }
    await loadSources();
    ElMessage.success("素材源已保存并启用");
  } finally {
    saving.value = false;
  }
}

async function enableSource(row: MaterialSource) {
  await api.saveMaterialSource({
    name: row.name,
    url: row.url,
    source_type: row.source_type,
    enabled: true,
  });
  await loadSources();
  ElMessage.success("素材源已启用");
}

async function checkSource(sourceId: number) {
  const result = await api.checkMaterialSource(sourceId);
  await refresh();
  if (result.error) ElMessage.error(result.error);
  else ElMessage.success(`找到 ${result.found || 0} 条，新增 ${result.inserted || 0} 条`);
}

async function checkAll() {
  checkingAll.value = true;
  try {
    if (!enabledSources.value.length) {
      if (activeType.value === "binance_square") {
        ElMessage.warning("请先保存至少一个 BN 广场作者源");
        return;
      }
      await saveSource();
    }
    for (const source of enabledSources.value) {
      await api.checkMaterialSource(source.id);
    }
    await refresh();
    ElMessage.success("当前类型采集完成");
  } finally {
    checkingAll.value = false;
  }
}

async function disableSource(sourceId: number) {
  await ElMessageBox.confirm("确认停用这个素材源？", "停用素材源", { type: "warning" });
  await api.deleteMaterialSource(sourceId);
  await loadSources();
  ElMessage.success("素材源已停用");
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
    const nextType = sourceTypes.includes(value as SourceType)
      ? (value as SourceType)
      : activeType.value;
    if (nextType !== activeType.value) {
      activeType.value = nextType;
      applyPreset();
      await loadItems();
    }
    await nextTick();
    document
      .getElementById(section === "items" ? "source-items" : "source-config")
      ?.scrollIntoView({ block: "start" });
  },
);

onMounted(async () => {
  applyPreset();
  await refresh();
});
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
  margin: 6px 0 0;
  overflow: hidden;
  color: var(--text-secondary);
  line-height: 1.55;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

@media (max-width: 900px) {
  .source-form {
    grid-template-columns: 1fr;
  }

  .source-form .wide {
    grid-column: auto;
  }
}
</style>
