import { createRouter, createWebHashHistory } from "vue-router";

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/dashboard" },
    {
      path: "/dashboard",
      name: "dashboard",
      component: () => import("@/views/Dashboard.vue"),
    },
    {
      path: "/accounts",
      name: "accounts",
      component: () => import("@/views/Accounts.vue"),
    },
    {
      path: "/performance",
      name: "performance",
      component: () => import("@/views/Performance.vue"),
    },
    {
      path: "/history",
      name: "history",
      component: () => import("@/views/History.vue"),
    },
    {
      path: "/sources",
      name: "sources",
      component: () => import("@/views/Sources.vue"),
    },
    {
      path: "/settings",
      name: "settings",
      component: () => import("@/views/Settings.vue"),
    },
  ],
});
