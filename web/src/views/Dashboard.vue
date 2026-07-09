<template>
  <div class="dashboard">
    <div class="toolbar">
      <div class="toolbar-title">
        <strong>自动运行</strong>
        <span>采集、打标、消费、发布的后台循环状态</span>
      </div>
      <el-space wrap>
        <el-button :type="monitor?.auto_monitor_enabled ? 'warning' : 'primary'" @click="toggleMonitor">
          {{ monitor?.auto_monitor_enabled ? "暂停自动运行" : "启动自动运行" }}
        </el-button>
        <el-button type="primary" plain :loading="runningOnce" @click="runOnce">立即运行</el-button>
        <el-button plain :loading="checkingMcp" @click="checkMcp">检查发布通道</el-button>
      </el-space>
    </div>

    <div class="metric-grid">
      <div class="metric-card">
        <strong>{{ monitor?.auto_monitor_enabled ? (monitor.running ? "运行中" : "等待下一轮") : "已暂停" }}</strong>
        <span>后台服务</span>
      </div>
      <div class="metric-card">
        <strong>{{ monitor ? `${monitor.poll_interval_seconds}/${monitor.success_interval_seconds}/${monitor.failure_interval_seconds}s` : "-" }}</strong>
        <span>运行节奏</span>
      </div>
      <div class="metric-card">
        <strong>{{ monitor?.auto_consume_materials ? "开启" : "关闭" }}</strong>
        <span>自动消费</span>
      </div>
      <div class="metric-card">
        <strong :title="monitor?.current_stage || ''">{{ shortText(monitor?.current_stage || "空闲") }}</strong>
        <span>当前阶段</span>
      </div>
      <div class="metric-card">
        <strong>{{ nextRunLabel(monitor) }}</strong>
        <span>下一轮</span>
      </div>
      <div class="metric-card">
        <strong>{{ monitor?.consecutive_publish_failures || 0 }}/{{ monitor?.publish_failure_alert_threshold || 5 }}</strong>
        <span>连续失效</span>
      </div>
    </div>

    <el-card class="page-card log-card" shadow="never">
      <template #header>
        <div class="toolbar">
          <div class="toolbar-title">
            <strong>运行日志</strong>
            <span>自动循环最近一轮详细输出</span>
          </div>
          <el-button text @click="loadMonitor">刷新</el-button>
        </div>
      </template>
      <pre class="log-box">{{ formatMonitorLogs(monitor) }}</pre>
    </el-card>

    <el-dialog v-model="mcpDialogVisible" title="发布通道检查" width="720px">
      <pre class="dialog-json">{{ mcpResult }}</pre>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ElMessage } from "element-plus";
import { onMounted, onUnmounted, ref } from "vue";

import { api } from "@/api";
import type { MonitorStatus } from "@/types";
import { formatMonitorLogs, nextRunLabel, shortText } from "@/utils";

const monitor = ref<MonitorStatus | null>(null);
const runningOnce = ref(false);
const checkingMcp = ref(false);
const mcpDialogVisible = ref(false);
const mcpResult = ref("");
let timer: number | undefined;

async function loadMonitor() {
  monitor.value = await api.monitor();
}

async function toggleMonitor() {
  const enabled = !monitor.value?.auto_monitor_enabled;
  await api.setMonitorEnabled(enabled);
  await loadMonitor();
  ElMessage.success(enabled ? "自动运行已启动" : "自动运行已暂停");
}

async function runOnce() {
  runningOnce.value = true;
  try {
    await api.runMaterialMonitor();
    await loadMonitor();
    ElMessage.success("本轮运行完成");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "运行失败");
  } finally {
    runningOnce.value = false;
  }
}

async function checkMcp() {
  checkingMcp.value = true;
  try {
    const result = await api.mcpTools();
    mcpResult.value = JSON.stringify(result, null, 2);
    mcpDialogVisible.value = true;
  } finally {
    checkingMcp.value = false;
  }
}

onMounted(() => {
  loadMonitor();
  timer = window.setInterval(() => loadMonitor().catch(() => undefined), 30000);
});

onUnmounted(() => {
  if (timer) window.clearInterval(timer);
});
</script>

<style scoped>
.dashboard {
  display: grid;
  gap: 16px;
}

.log-card :deep(.el-card__header) {
  padding-bottom: 0;
}

.dialog-json {
  max-height: 520px;
  overflow: auto;
  white-space: pre-wrap;
  font-family: Consolas, "SFMono-Regular", monospace;
  font-size: 12px;
}
</style>
