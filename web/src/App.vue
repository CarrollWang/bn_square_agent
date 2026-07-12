<template>
  <el-config-provider :locale="zhCn">
    <el-container class="app-shell">
      <el-aside width="224px" class="app-sidebar">
        <div class="brand">
          <div class="brand-mark">BN</div>
          <div class="brand-copy">
            <strong>BN Square Agent</strong>
            <span>自动运营控制台</span>
          </div>
        </div>
        <el-menu
          :default-active="activeMenu"
          class="side-menu"
          background-color="#131821"
          text-color="#b8c0cc"
          active-text-color="#6b96ff"
          router
        >
          <el-menu-item index="/dashboard">
            <el-icon><Monitor /></el-icon>
            <span>自动运行</span>
          </el-menu-item>
          <el-menu-item index="/accounts">
            <el-icon><User /></el-icon>
            <span>账号管理</span>
          </el-menu-item>
          <el-menu-item index="/performance">
            <el-icon><DataAnalysis /></el-icon>
            <span>账号表现</span>
          </el-menu-item>
          <el-menu-item index="/history">
            <el-icon><Document /></el-icon>
            <span>发文历史</span>
          </el-menu-item>
          <el-sub-menu index="sources">
            <template #title>
              <el-icon><FolderOpened /></el-icon>
              <span>素材中心</span>
            </template>
            <el-menu-item index="/sources?section=config&type=news_feed">新闻源配置</el-menu-item>
            <el-menu-item index="/sources?section=items&type=news_feed">新闻素材库</el-menu-item>
          </el-sub-menu>
          <el-sub-menu index="settings">
            <template #title>
              <el-icon><Setting /></el-icon>
              <span>系统设置</span>
            </template>
            <el-menu-item index="/settings?tab=llm">大模型设置</el-menu-item>
            <el-menu-item index="/settings?tab=alerts">邮箱预警设置</el-menu-item>
            <el-menu-item index="/settings?tab=runtime">自动运行设置</el-menu-item>
          </el-sub-menu>
        </el-menu>
      </el-aside>

      <el-container>
        <el-header class="app-header">
          <div>
            <h1>{{ pageTitle }}</h1>
            <p>多账号采集、改写、配图、发布自动运营台</p>
          </div>
          <div class="channel-status">MCP 发布通道</div>
        </el-header>
        <el-main class="app-main">
          <router-view />
        </el-main>
      </el-container>
    </el-container>
  </el-config-provider>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import {
  DataAnalysis,
  Document,
  FolderOpened,
  Monitor,
  Setting,
  User,
} from "@element-plus/icons-vue";
import zhCn from "element-plus/es/locale/lang/zh-cn";

const route = useRoute();

const pageTitle = computed(() => {
  const map: Record<string, string> = {
    dashboard: "自动运行",
    accounts: "账号管理",
    performance: "账号表现",
    history: "发文历史",
    sources: "素材中心",
    settings: "系统设置",
  };
  return map[String(route.name || "dashboard")] || "BN Square Agent";
});

const activeMenu = computed(() => {
  if (route.name === "sources") {
    return `/sources?section=${route.query.section || "config"}&type=news_feed`;
  }
  if (route.name === "settings") {
    return `/settings?tab=${route.query.tab || "llm"}`;
  }
  return route.path;
});
</script>
