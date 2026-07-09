import { createRouter, createWebHashHistory } from "vue-router";

import Accounts from "@/views/Accounts.vue";
import Dashboard from "@/views/Dashboard.vue";
import History from "@/views/History.vue";
import Performance from "@/views/Performance.vue";
import Settings from "@/views/Settings.vue";
import Sources from "@/views/Sources.vue";

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/dashboard" },
    { path: "/dashboard", name: "dashboard", component: Dashboard },
    { path: "/accounts", name: "accounts", component: Accounts },
    { path: "/performance", name: "performance", component: Performance },
    { path: "/history", name: "history", component: History },
    { path: "/sources", name: "sources", component: Sources },
    { path: "/settings", name: "settings", component: Settings },
  ],
});
